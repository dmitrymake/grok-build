"""State mutation transactions."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from grokbuild.decision import ExecutionStage
from grokbuild.evidence import (
    EvidenceRecord,
    FailureSignal,
    classify_failure,
    make_evidence,
    persist_evidence,
)
from grokbuild.persist import append_jsonl, atomic_update_json
from grokbuild.verifiers import is_verifier_command
from grokbuild.remediation import record_remediation_attempt
from grokbuild.state import (
    DEFAULT_CONSILIUM_THRESHOLD,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    ExecutionTrack,
    RuntimeState,
    state_from_raw,
    _migrate_legacy_history,
    _with_stage_slots,
    default_log_path,
    default_state_path,
)


def _transaction(path, mutate, *, append_decision=None):
    target = Path(path) if path is not None else default_state_path()

    def updater(raw):
        state = state_from_raw(raw, target)
        state.prune()
        mutate(state)
        return state.to_dict()

    return atomic_update_json(
        target,
        updater,
        default=RuntimeState(source_path=target).to_dict(),
        on_load=lambda raw: _migrate_legacy_history(raw, target),
        append_decision=append_decision,
    )


transaction = _transaction

__all__ = [
    "_transaction",
    "transaction",
    "resolve_task_binding",
    "allocate_turn",
    "ensure_turn",
    "record_decision_tx",
    "ensure_execution_tx",
    "allocate_parallel_member_tx",
    "bind_parallel_member_task_tx",
    "release_unresolvable_binding_tx",
    "bind_linear_stage_task_tx",
    "bind_debt_stage_task_tx",
    "bind_terminal_task_tx",
    "bind_verify_task_tx",
    "bind_debt_verify_task_tx",
    "record_stop_block_tx",
    "record_stage_tx",
    "record_retrieval_result_tx",
    "record_review_inconclusive_tx",
    "record_verify_retrieval_tx",
    "record_session_debt_stage_tx",
    "record_verify_fanout_tx",
    "record_verify_tx",
]


def _reconcile_execution_track(track: ExecutionTrack, stages: tuple[ExecutionStage, ...]) -> None:
    old_stages = _with_stage_slots(track.stages)
    new_stages = _with_stage_slots(stages)
    dropped: list[str] = []

    def stage_matches(
        key: str, candidates: tuple[ExecutionStage, ...]
    ) -> list[tuple[ExecutionStage, str | None]]:
        matches: list[tuple[ExecutionStage, str | None]] = []
        for stage in candidates:
            if stage.kind == "parallel_spawn":
                for member in stage.members:
                    member_key = track.member_key(stage.stage_id, member.member_id)
                    if key == member_key:
                        matches.append((stage, member.member_id))
            elif stage.kind == "verify":
                if key == (stage.stage_id or stage.role):
                    matches.append((stage, None))
            elif key == stage.role:
                matches.append((stage, None))
        return matches

    def migrate_key(key: str, field_name: str) -> str | None:
        old_matches = stage_matches(key, old_stages)
        if len(old_matches) != 1:
            dropped.append(f"{field_name}:{key}")
            return None
        old_stage, member_id = old_matches[0]
        counterparts = [
            stage
            for stage in new_stages
            if stage.slot == old_stage.slot and stage.kind == old_stage.kind
        ]
        if len(counterparts) != 1:
            dropped.append(f"{field_name}:{key}")
            return None
        counterpart = counterparts[0]
        if counterpart.kind == "parallel_spawn":
            if not any(member.member_id == member_id for member in counterpart.members):
                dropped.append(f"{field_name}:{key}")
                return None
            return track.member_key(counterpart.stage_id, str(member_id))
        if counterpart.kind == "verify":
            return counterpart.stage_id or counterpart.role
        return counterpart.role

    def migrate_list(values: list[str], field_name: str) -> list[str]:
        result: list[str] = []
        for key in values:
            migrated = migrate_key(key, field_name)
            if migrated is not None and migrated not in result:
                result.append(migrated)
        return result

    def migrate_keyed(values: Mapping[str, Any], field_name: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values.items():
            migrated = migrate_key(key, field_name)
            if migrated is not None:
                result[migrated] = value
        return result

    def migrate_valued(values: Mapping[str, str], field_name: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for binding, key in values.items():
            migrated = migrate_key(key, field_name)
            if migrated is not None:
                result[binding] = migrated
        return result

    track.stages = new_stages
    track.requested = migrate_list(track.requested, "requested")
    track.completed = migrate_list(track.completed, "completed")
    track.failed = migrate_list(track.failed, "failed")
    track.verified = migrate_list(track.verified, "verified")
    track.member_tasks = migrate_keyed(track.member_tasks, "member_tasks")
    track.stage_tasks = migrate_keyed(track.stage_tasks, "stage_tasks")
    track.terminal_tasks = migrate_valued(track.terminal_tasks, "terminal_tasks")
    track.verify_tasks = migrate_valued(track.verify_tasks, "verify_tasks")
    track.released_verify_tasks = migrate_valued(
        track.released_verify_tasks, "released_verify_tasks"
    )
    track.requested_at = migrate_keyed(track.requested_at, "requested_at")
    track.repair_failures = migrate_keyed(track.repair_failures, "repair_failures")
    if dropped:
        track.migrated_dropped.extend(
            item for item in dropped if item not in track.migrated_dropped
        )
        warning = f"execution evidence migration dropped {len(dropped)} unmatched or ambiguous entr{'y' if len(dropped) == 1 else 'ies'}"
        if warning not in track.warnings:
            track.warnings.append(warning)


def _record_repair_result(
    track: ExecutionTrack,
    stage_key: str,
    success: bool,
    consilium_after_failures: int,
) -> tuple[dict[str, Any] | None, Any | None]:
    if success:
        track.repair_failures[stage_key] = 0
        return None, None
    count = track.repair_failures.get(stage_key, 0) + 1
    track.repair_failures[stage_key] = count
    if count < max(1, consilium_after_failures):
        return None, None
    if any(stage.stage_id == "consilium" for stage in track.stages):
        return None, None
    from grokbuild.compose import compose_consilium_barrier
    from grokbuild.roles import load_registry

    consilium = compose_consilium_barrier(load_registry())
    if consilium is not None:
        without_sentinel = tuple(
            stage for stage in track.stages if stage.stage_id != "consilium-unavailable"
        )
        _reconcile_execution_track(track, without_sentinel + (consilium,))
        return None, None
    from grokbuild.evidence import FailureSignal, make_evidence

    signal = FailureSignal(reason="required cross-provider repair barrier could not be composed")
    warning = "CONSILIUM UNAVAILABLE: required cross-provider repair barrier could not be composed"
    if not any(stage.stage_id == "consilium-unavailable" for stage in track.stages):
        sentinel = ExecutionStage(
            role="consilium-unavailable",
            required=True,
            reason=warning,
            kind="sentinel",
            stage_id="consilium-unavailable",
            slot="consilium",
        )
        track.stages += (sentinel,)
    if warning not in track.warnings:
        track.warnings.append(warning)
        log_record = {
            "event": "consilium_unavailable",
            "decision_id": track.decision_id,
            "warning": warning,
            "observed_at": time.time(),
        }
        evidence = make_evidence(
            decision_id=track.decision_id,
            session_id=track.session_id,
            subject={"path": "consilium", "stage": stage_key},
            signal=signal,
        )
        return log_record, evidence
    return None, None


def _flush_repair_side_effects(log_record: dict[str, Any] | None, evidence: Any | None) -> None:
    if log_record is not None:
        append_jsonl(default_log_path(), log_record)
    if evidence is not None:
        persist_evidence(evidence)


def _task_bindings(
    state: RuntimeState,
    session_id: str,
    current_decision_id: str | None,
    task_id: str,
) -> dict[tuple[str, str], tuple[ExecutionTrack, str]]:
    """Resolve every stage a background task id is bound to, newest track first.

    One matcher for every consumer: an independent lookup would be free to
    disagree with the transaction that actually mutates the track, and a task
    id resolving to two different stages must stay ambiguous everywhere.
    """
    current = state.get_execution(current_decision_id) if current_decision_id else None
    tracks: list[tuple[str, ExecutionTrack]] = []
    if current is not None and current.session_id == session_id:
        tracks.append((current.decision_id, current))
    tracks.extend(
        sorted(
            (
                (decision_id, track)
                for decision_id, track in state.executions.items()
                if decision_id != current_decision_id and track.session_id == session_id
            ),
            key=lambda item: (item[1].updated_at, item[0]),
            reverse=True,
        )
    )
    matches: dict[tuple[str, str], tuple[ExecutionTrack, str]] = {}
    for decision_id, track in tracks:
        for stage_key, bound_id in track.member_tasks.items():
            if bound_id == task_id:
                matches[(decision_id, stage_key)] = (track, "member")
        for stage_key, bound_id in track.stage_tasks.items():
            if bound_id == task_id:
                matches[(decision_id, stage_key)] = (track, "linear")
        stage_key = track.terminal_tasks.get(task_id)
        if stage_key is not None:
            # A live member binding wins: downgrading it to ``terminal`` would
            # disable the auto-release branch and strand the member.
            matches.setdefault((decision_id, stage_key), (track, "terminal"))
        stage_key = track.verify_tasks.get(task_id)
        if stage_key is not None:
            matches.setdefault((decision_id, stage_key), (track, "verify"))
        stage_key = track.released_verify_tasks.get(task_id)
        if stage_key is not None:
            matches.setdefault((decision_id, stage_key), (track, "verify"))
    return matches


def resolve_task_binding(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    task_id: str,
) -> tuple[str, str] | None:
    """Return the single ``(decision_id, stage_key)`` a task id resolves to."""
    if not session_id or not task_id:
        return None
    from grokbuild.state import load_state

    target = Path(path) if path is not None else default_state_path()
    matches = _task_bindings(load_state(target), session_id, current_decision_id, task_id)
    if len(matches) != 1:
        return None
    (decision_id, stage_key), _binding = next(iter(matches.items()))
    return decision_id, stage_key


def allocate_turn(
    path: Path | str | None = None,
    session_id: str | None = None,
    prompt_fingerprint: str | None = None,
) -> int | None:
    """Allocate the next turn id for a session under a single flock.

    When ``prompt_fingerprint`` is supplied the allocation is idempotent per
    prompt: re-resolving the same turn returns the existing id instead of
    double-incrementing, while a new prompt advances the counter even if the
    ``UserPromptSubmit`` hook was missed.
    """

    if not session_id:
        return None
    target = Path(path) if path is not None else default_state_path()

    def mutate(state: RuntimeState) -> Any:
        if prompt_fingerprint is not None and state.prompts.get(session_id) == prompt_fingerprint:
            return None
        turn = state.turns.get(session_id, 0) + 1
        state.turns[session_id] = turn
        if prompt_fingerprint is not None:
            state.prompts[session_id] = prompt_fingerprint
        state.turn_seen[session_id] = time.time()
        state.updated_at = time.time()
        return None

    new_data = _transaction(target, mutate)
    return int(RuntimeState.from_dict(new_data).turns.get(session_id, 0))


def ensure_turn(
    path: Path | str | None = None,
    session_id: str | None = None,
    turn: int | None = None,
) -> int | None:
    """Persist a transcript-derived turn ordinal (never decreases the counter)."""

    if not session_id or turn is None:
        return None
    target = Path(path) if path is not None else default_state_path()

    def mutate(state: RuntimeState) -> Any:
        state.turns[session_id] = max(state.turns.get(session_id, 0), int(turn))
        state.turn_seen[session_id] = time.time()
        state.updated_at = time.time()
        return None

    new_data = _transaction(target, mutate)
    return int(RuntimeState.from_dict(new_data).turns.get(session_id, 0))


def record_decision_tx(
    path: Path | str | None, decision_dict: Mapping[str, Any], role: str | None
) -> None:
    """Record a decision + its execution track under one flock."""

    target = Path(path) if path is not None else default_state_path()

    def mutate(state: RuntimeState) -> Any:
        state.record_decision(decision_dict)
        if role:
            state.record_request(role)
        stages = tuple(
            ExecutionStage.from_dict(item)
            if isinstance(item, Mapping)
            else ExecutionStage("", False, "")
            for item in (decision_dict.get("execution") or [])
        )
        decision_id = str(decision_dict.get("decision_id") or "")
        session_id = decision_dict.get("session_id")
        turn_id = decision_dict.get("turn_id")
        track = state.get_execution(decision_id)
        if track is None:
            state.set_execution(decision_id, session_id, turn_id, stages)
        elif track.stages != stages:
            runtime_stages = tuple(
                stage
                for stage in track.stages
                if stage.stage_id
                in {
                    "consilium",
                    "consilium-unavailable",
                    # Composed after a comparison could not be settled, so it is
                    # absent from the route and would otherwise be reconciled away.
                    "evidence-collection",
                    "judge-adjudication",
                }
                and not any(candidate.stage_id == stage.stage_id for candidate in stages)
            )
            _reconcile_execution_track(track, stages + runtime_stages)
            track.session_id = session_id
            track.turn_id = turn_id
            track.updated_at = time.time()
        return None

    _transaction(
        target,
        mutate,
        append_decision=decision_dict,
    )


def ensure_execution_tx(
    path: Path | str | None,
    decision_id: str,
    session_id: str | None,
    turn_id: int | None,
    stages: Any,
) -> None:
    """Create or reconcile an execution track under one state-file lock."""

    target = Path(path) if path is not None else default_state_path()
    parsed = _with_stage_slots(
        tuple(
            ExecutionStage.from_dict(item)
            if isinstance(item, Mapping)
            else ExecutionStage("", False, "")
            for item in (stages or [])
        )
    )

    def mutate(state: RuntimeState) -> Any:
        track = state.get_execution(decision_id)
        if track is None:
            state.set_execution(decision_id, session_id, turn_id, parsed)
        elif track.stages != parsed:
            runtime_stages = tuple(
                stage
                for stage in track.stages
                if stage.stage_id
                in {
                    "consilium",
                    "consilium-unavailable",
                    # Composed after a comparison could not be settled, so it is
                    # absent from the route and would otherwise be reconciled away.
                    "evidence-collection",
                    "judge-adjudication",
                }
                and not any(candidate.stage_id == stage.stage_id for candidate in parsed)
            )
            _reconcile_execution_track(track, parsed + runtime_stages)
            track.session_id = session_id
            track.turn_id = turn_id
            track.updated_at = time.time()
        return None

    _transaction(target, mutate)


def allocate_parallel_member_tx(
    path: Path | str | None,
    decision_id: str,
    role: str,
    stage_id: str | None = None,
) -> str | None:
    """Atomically reserve one matching parallel member for a spawn request.

    A requested member without a recorded background id is unbound: its spawn
    acknowledgement may have been lost, so a later same-role request may claim
    it again. Recorded ids remain exclusive until retrieval resolves them.
    """
    target = Path(path) if path is not None else default_state_path()
    allocated: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal allocated
        now = time.time()
        track = state.get_execution(decision_id)
        if track is not None:
            for stage in track.stages:
                if stage.kind != "parallel_spawn" or (
                    stage_id is not None and stage.stage_id != stage_id
                ):
                    continue
                candidates = [
                    track.member_key(stage.stage_id, member.member_id)
                    for member in track.incomplete_required_members(stage)
                    if member.role == role
                ]
                # Prefer never-requested members, then reclaim an unbound
                # request only when no new same-role shard remains.
                for key in candidates:
                    if key not in track.requested:
                        track.requested.append(key)
                        track.requested_at.setdefault(key, now)
                        track.updated_at = now
                        allocated = key
                        return None
                for key in candidates:
                    if key not in track.member_tasks:
                        allocated = key
                        return None
        return None

    _transaction(target, mutate)
    return allocated


def bind_parallel_member_task_tx(
    path: Path | str | None,
    decision_id: str,
    role: str,
    task_id: str,
) -> str | None:
    """Atomically bind a background task id to one requested parallel member.

    Repeated acknowledgements for the same task id are idempotent. A distinct
    task id can only claim an unbound matching member, preventing concurrent
    PostToolUse hooks from overwriting an existing binding.
    """
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        track = state.get_execution(decision_id)
        if track is not None:
            for key, bound_task_id in track.member_tasks.items():
                if bound_task_id == task_id:
                    member = track.member_for_key(key)
                    if member is not None and member.role == role:
                        bound = key
                    return None
            released_key = track.terminal_tasks.get(task_id)
            released_member = (
                track.member_for_key(released_key) if released_key is not None else None
            )
            if released_member is not None:
                if released_member.role == role:
                    bound = released_key
                return None
            # The id already correlates to a released member; re-binding it to a
            # different shard would make every later retrieval ambiguous.
            for stage in track.stages:
                if stage.kind != "parallel_spawn":
                    continue
                for member in stage.members:
                    key = track.member_key(stage.stage_id, member.member_id)
                    if (
                        member.role == role
                        and key in track.requested
                        and key not in track.completed
                        and key not in track.member_tasks
                    ):
                        track.member_tasks[key] = task_id
                        track.requested_at.setdefault(key, time.time())
                        track.updated_at = time.time()
                        bound = key
                        return None
        return None

    _transaction(target, mutate)
    return bound


def release_unresolvable_binding_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    task_id: str,
) -> str:
    """Record a missing lookup and release any binding after two matches."""
    if not session_id or not task_id:
        return "unmatched"
    target = Path(path) if path is not None else default_state_path()
    outcome = "unmatched"

    def mutate(state: RuntimeState) -> Any:
        nonlocal outcome
        matches = _task_bindings(state, session_id, current_decision_id, task_id)
        if len(matches) > 1 and not all(binding == "verify" for _, binding in matches.values()):
            outcome = "ambiguous"
            return None
        if not matches:
            return None

        for (_decision_id, stage_key), (track, binding_kind) in matches.items():
            previous = track.not_found_streaks.get(stage_key)
            if previous is not None and previous.get("task_id") != task_id:
                previous = {"task_id": task_id, "count": 0}
            count = int(previous.get("count", 0)) + 1 if previous else 1
            track.not_found_streaks[stage_key] = {"task_id": task_id, "count": count}
            track.updated_at = time.time()
            outcome = "streak"
            if count < 2 or binding_kind not in {"member", "linear", "terminal", "verify"}:
                continue
            # Keep the task correlation after dropping the binding, so a late
            # retrieval for the same id still resolves to this stage.
            if binding_kind == "verify":
                track.released_verify_tasks[task_id] = stage_key
            else:
                track.terminal_tasks[task_id] = stage_key
            current_member = track.member_tasks.get(stage_key)
            current_stage = track.stage_tasks.get(stage_key)
            current_verify = next(
                (bound_id for bound_id, key in track.verify_tasks.items() if key == stage_key),
                None,
            )
            if any(
                bound_id is not None and bound_id != task_id
                for bound_id in (current_member, current_stage, current_verify)
            ):
                track.not_found_streaks.pop(stage_key, None)
                continue
            track.member_tasks.pop(stage_key, None)
            track.stage_tasks.pop(stage_key, None)
            track.verify_tasks.pop(task_id, None)
            if stage_key in track.requested:
                track.requested.remove(stage_key)
            track.requested_at.pop(stage_key, None)
            if stage_key not in track.failed:
                track.failed.append(stage_key)
            track.failure_reasons[stage_key] = "task_not_found"
            track.not_found_streaks.pop(stage_key, None)
            track.updated_at = time.time()
            outcome = "released"
        return None

    _transaction(target, mutate)
    return outcome


def bind_linear_stage_task_tx(
    path: Path | str | None,
    decision_id: str,
    role: str,
    task_id: str,
) -> str | None:
    """Atomically bind a background task id to one requested linear stage."""
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        track = state.get_execution(decision_id)
        if track is not None:
            for stage_role, bound_task_id in track.stage_tasks.items():
                if bound_task_id == task_id:
                    bound = stage_role
                    return None
            for stage in track.stages:
                if (
                    stage.kind != "parallel_spawn"
                    and stage.required
                    and stage.spawnable
                    and stage.role == role
                    and role not in track.completed
                    and role not in track.stage_tasks
                    and role in track.requested
                ):
                    track.stage_tasks[role] = task_id
                    track.updated_at = time.time()
                    bound = role
                    return None
        return None

    _transaction(target, mutate)
    return bound


def bind_debt_stage_task_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    role: str,
    task_id: str,
    window_seconds: float,
) -> str | None:
    """Atomically bind a background task to one unambiguous session debt stage.

    Unbound same-role members of ONE parallel barrier are not ambiguous: the
    lowest member key is claimed, mirroring ``allocate_parallel_member_tx``.
    Matches spanning several tracks or several stages stay a strict no-op, and
    a retry of an already-bound task id reuses its binding unmutated.
    """
    if not session_id or not role or not task_id or window_seconds < 0:
        return None
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        now = time.time()
        claims: list[tuple[ExecutionTrack, list[list[str]]]] = []
        tracks = sorted(
            state.executions.items(),
            key=lambda item: (item[1].updated_at, item[0]),
            reverse=True,
        )
        existing_bindings = [
            (track, key)
            for _decision_id, track in tracks
            if track.session_id == session_id
            for bindings in (track.member_tasks, track.stage_tasks)
            for key, bound_id in bindings.items()
            if bound_id == task_id
        ]
        existing_bindings.extend(
            (track, key)
            for _decision_id, track in tracks
            if track.session_id == session_id
            for bound_id, key in track.terminal_tasks.items()
            # A member correlation parked by the release path is still a binding:
            # claiming a second stage for the same id would make it ambiguous.
            if bound_id == task_id and track.member_for_key(key) is not None
        )
        if len(existing_bindings) == 1:
            bound = existing_bindings[0][1]
            return None
        if len(existing_bindings) > 1:
            return None
        for decision_id, track in tracks:
            if (
                decision_id == current_decision_id
                or track.session_id != session_id
                or track.all_required_completed()
                or now - track.updated_at > window_seconds
            ):
                continue
            stage_keys: list[list[str]] = []
            for stage in track.stages:
                if not stage.required or not stage.spawnable:
                    continue
                if stage.kind == "parallel_spawn":
                    member_keys = [
                        track.member_key(stage.stage_id, member.member_id)
                        for member in track.incomplete_required_members(stage)
                        if member.role == role
                        and track.member_key(stage.stage_id, member.member_id)
                        not in track.member_tasks
                    ]
                    if member_keys:
                        stage_keys.append(member_keys)
                elif (
                    stage.role == role
                    and role not in track.completed
                    and role not in track.stage_tasks
                ):
                    stage_keys.append([role])
            if stage_keys:
                claims.append((track, stage_keys))
        if len(claims) != 1:
            return None
        track, stage_keys = claims[0]
        if len(stage_keys) > 1:
            return None
        keys = stage_keys[0]
        key = min(keys)
        if track.member_for_key(key) is not None and key not in track.requested:
            track.requested.append(key)
            track.requested_at.setdefault(key, now)
        if track.member_for_key(key) is not None:
            track.member_tasks[key] = task_id
        else:
            track.stage_tasks[key] = task_id
        track.updated_at = now
        bound = key
        return None

    _transaction(target, mutate)
    return bound


def bind_terminal_task_tx(
    path: Path | str | None,
    decision_id: str,
    task_id: str,
    stage_key: str,
) -> str | None:
    """Atomically bind a background terminal task to an execution stage."""
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        track = state.get_execution(decision_id)
        if track is not None:
            track.terminal_tasks[task_id] = stage_key
            track.updated_at = time.time()
            bound = stage_key
        return None

    _transaction(target, mutate)
    return bound


def bind_verify_task_tx(
    path: Path | str | None,
    decision_id: str,
    task_id: str,
    step: str,
) -> str | None:
    """Atomically bind a background task id to a deterministic verify step."""
    if not task_id or not step:
        return None
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        track = state.get_execution(decision_id)
        valid_steps = (
            {stage.stage_id or stage.role for stage in track.stages if not stage.spawnable}
            if track is not None
            else set()
        )
        if track is not None and step in valid_steps:
            track.verify_tasks[task_id] = step
            track.updated_at = time.time()
            bound = step
        return None

    _transaction(target, mutate)
    return bound


def bind_debt_verify_task_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    command: str | None,
    task_id: str,
    window_seconds: float,
) -> str | None:
    """Bind a verifier task to every same-session debt track owing that command.

    A deterministic verifier is command-scoped, not track-scoped: its result
    applies to every in-window debt track whose next verify step configures the
    exact same command. Requiring a single match would leave the remaining
    tracks permanently unclosable, so every match binds the same task_id.
    """
    if not session_id or not command or not task_id or window_seconds < 0:
        return None
    target = Path(path) if path is not None else default_state_path()
    bound: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal bound
        now = time.time()
        matches: list[tuple[str, ExecutionTrack, str]] = []
        for decision_id, track in sorted(
            state.executions.items(),
            key=lambda item: (item[1].updated_at, item[0]),
            reverse=True,
        ):
            if (
                decision_id == current_decision_id
                or track.session_id != session_id
                or now - track.updated_at > window_seconds
                or track.next_required_barrier_or_role() is not None
            ):
                continue
            step = track.next_verify_step()
            if not step or step in track.verify_tasks.values():
                continue
            expected = next(
                (
                    stage.command
                    for stage in track.stages
                    if (stage.stage_id or stage.role) == step and not stage.spawnable
                ),
                "",
            )
            if is_verifier_command(command, expected):
                matches.append((decision_id, track, step))
        if not matches:
            return None
        for _decision_id, track, step in matches:
            track.verify_tasks[task_id] = step
            track.updated_at = now
        bound = matches[0][2]
        return None

    _transaction(target, mutate)
    return bound


def record_stop_block_tx(path: Path | str | None, decision_id: str) -> None:
    """Increment a track's bounded Stop nudge count under one lock."""
    target = Path(path) if path is not None else default_state_path()

    def mutate(state: RuntimeState) -> Any:
        track = state.get_execution(decision_id)
        if track is not None:
            track.stop_blocks += 1
            track.updated_at = time.time()
        return None

    _transaction(target, mutate)


def _apply_stage_result(
    state: RuntimeState,
    track: ExecutionTrack,
    key: str,
    success: bool,
    *,
    actual_role: str,
    decision_id: str,
    subject: dict[str, str] | None,
    failure_signal: FailureSignal | None,
    threshold: int,
    cooldown: float,
    escalate: bool,
    create_evidence: bool,
    remove_completed_on_failure: bool,
    clear_not_found_on_failure: bool,
    reset_stop_blocks: str,
) -> EvidenceRecord | None:
    made_progress = False
    evidence: EvidenceRecord | None = None
    if success:
        if key not in track.completed:
            track.completed.append(key)
            made_progress = True
        if key in track.failed:
            track.failed.remove(key)
        state.mark_success(actual_role, session_id=track.session_id)
    else:
        if key not in track.failed:
            track.failed.append(key)
        if remove_completed_on_failure and key in track.completed:
            track.completed.remove(key)
        if track.member_for_key(key) and key in track.requested:
            track.requested.remove(key)
        track.member_tasks.pop(key, None)
        if clear_not_found_on_failure:
            track.not_found_streaks.pop(key, None)
        signal = failure_signal or FailureSignal(reason="spawn result failure")
        if create_evidence:
            evidence = make_evidence(
                decision_id=decision_id,
                session_id=track.session_id,
                subject=subject or {},
                signal=signal,
            )
        if escalate and classify_failure(signal) != "environment":
            state.mark_failure(
                actual_role,
                "spawn result failure",
                threshold=threshold,
                cooldown=cooldown,
                session_id=track.session_id,
            )
    if reset_stop_blocks == "success" and success:
        track.stop_blocks = 0
    elif reset_stop_blocks == "progress" and made_progress:
        track.stop_blocks = 0
    return evidence


def record_stage_tx(
    path: Path | str | None,
    decision_id: str,
    action: str,
    role: str | None = None,
    success: bool | None = None,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    consilium_after_failures: int = DEFAULT_CONSILIUM_THRESHOLD,
    task_id: str | None = None,
    retrieval_task_id: str | None = None,
    failure_signal: FailureSignal | None = None,
) -> None:
    """Advance the execution state machine for one decision under one flock.

    A successful spawn *result* also closes the role circuit breaker; a failed
    spawn result feeds it transactionally so that after ``threshold`` failures
    the role becomes observe-only instead of holding the session forever. The
    track's ``completed``/``failed`` lists record received results, not proof
    that a child verified its work.
    """

    target = Path(path) if path is not None else default_state_path()
    evidence: EvidenceRecord | None = None
    repair_log: dict[str, Any] | None = None
    repair_evidence: Any | None = None
    remediation_session: str | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal evidence, repair_log, repair_evidence, remediation_session
        track = state.get_execution(decision_id)
        if track is None:
            return None
        made_progress = False
        if action == "requested" and role:
            now = time.time()
            if role not in track.requested:
                track.requested.append(role)
            track.requested_at[role] = min(track.requested_at.get(role, now), now)
            if task_id:
                track.member_tasks[role] = task_id
            made_progress = True
        elif action == "result" and role:
            if success is False and role in track.completed and not retrieval_task_id:
                track.updated_at = time.time()
                return None
            remediation_session = track.session_id
            actual_role = track.member_for_key(role).role if track.member_for_key(role) else role
            track.stage_tasks.pop(actual_role, None)
            if retrieval_task_id and track.terminal_tasks.get(retrieval_task_id) == role:
                track.terminal_tasks.pop(retrieval_task_id, None)
            if task_id:
                track.member_tasks[role] = task_id
            track.requested_at.pop(role, None)
            repair_log, repair_evidence = _record_repair_result(
                track, role, bool(success), consilium_after_failures
            )
            evidence = _apply_stage_result(
                state,
                track,
                role,
                bool(success),
                actual_role=actual_role,
                decision_id=decision_id,
                subject={"stage": role, "role": actual_role},
                failure_signal=failure_signal,
                threshold=threshold,
                cooldown=cooldown,
                escalate=True,
                create_evidence=True,
                remove_completed_on_failure=True,
                clear_not_found_on_failure=True,
                reset_stop_blocks="progress",
            )
        if made_progress:
            track.stop_blocks = 0
        track.updated_at = time.time()
        return None

    _transaction(target, mutate)
    _flush_repair_side_effects(repair_log, repair_evidence)
    if evidence is not None:
        persist_evidence(evidence)
    if remediation_session is not None and action == "result" and role and success is not None:
        references = (f"evidence:{decision_id}:{role}",) if evidence is not None else ()
        record_remediation_attempt(
            session_id=remediation_session,
            decision_id=decision_id,
            branch=role,
            success=success,
            evidence_references=references,
        )


def record_retrieval_result_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    task_id: str,
    success: bool,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    consilium_after_failures: int = DEFAULT_CONSILIUM_THRESHOLD,
    escalate: bool = True,
    failure_signal: FailureSignal | None = None,
) -> str:
    """Resolve and record one retrieval result under one state-file lock."""
    if not session_id or not task_id:
        return "unmatched"
    target = Path(path) if path is not None else default_state_path()
    outcome = "unmatched"
    evidence: EvidenceRecord | None = None
    repair_log: dict[str, Any] | None = None
    repair_evidence: Any | None = None
    remediation_match: tuple[str, str, str] | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal evidence, repair_log, repair_evidence, outcome, remediation_match
        matches = _task_bindings(state, session_id, current_decision_id, task_id)
        if not matches:
            return None
        if len(matches) != 1:
            outcome = "ambiguous"
            return None

        (_decision_id, stage_key), (track, binding_kind) = next(iter(matches.items()))
        if binding_kind == "verify":
            return None
        actual_role = (
            track.member_for_key(stage_key).role if track.member_for_key(stage_key) else stage_key
        )
        current_owner = track.member_tasks.get(stage_key) or track.stage_tasks.get(actual_role)
        if current_owner is None:
            current_owner = next(
                (
                    bound_id
                    for bound_id, bound_key in track.terminal_tasks.items()
                    if bound_key == stage_key
                ),
                None,
            )
        if current_owner is not None and current_owner != task_id:
            track.terminal_tasks.pop(task_id, None)
            track.updated_at = time.time()
            outcome = "recorded"
            return None
        outcome = "recorded"
        remediation_match = (_decision_id, track.session_id or "", stage_key)
        track.not_found_streaks.pop(stage_key, None)
        if stage_key in track.completed:
            track.terminal_tasks.pop(task_id, None)
            return None

        actual_role = (
            track.member_for_key(stage_key).role if track.member_for_key(stage_key) else stage_key
        )
        track.stage_tasks.pop(actual_role, None)
        track.terminal_tasks.pop(task_id, None)
        track.requested_at.pop(stage_key, None)
        repair_log, repair_evidence = _record_repair_result(
            track, stage_key, success, consilium_after_failures
        )
        evidence = _apply_stage_result(
            state,
            track,
            stage_key,
            success,
            actual_role=actual_role,
            decision_id=_decision_id,
            subject={"stage": stage_key, "task": task_id, "role": actual_role},
            failure_signal=failure_signal,
            threshold=threshold,
            cooldown=cooldown,
            escalate=escalate,
            create_evidence=True,
            remove_completed_on_failure=False,
            clear_not_found_on_failure=False,
            reset_stop_blocks="progress",
        )
        track.updated_at = time.time()
        return None

    _transaction(target, mutate)
    _flush_repair_side_effects(repair_log, repair_evidence)
    if evidence is not None:
        persist_evidence(evidence)
    if remediation_match is not None:
        matched_decision, matched_session, matched_stage = remediation_match
        references = (f"evidence:{matched_decision}:{matched_stage}:{task_id}",) if evidence else ()
        record_remediation_attempt(
            session_id=matched_session,
            decision_id=matched_decision,
            branch=matched_stage,
            success=success,
            evidence_references=references,
        )
    return outcome


def record_review_inconclusive_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    task_id: str,
) -> str:
    """Discharge one uniquely bound review task without recording success or failure."""
    if not session_id or not task_id:
        return "unmatched"
    target = Path(path) if path is not None else default_state_path()
    outcome = "unmatched"

    def mutate(state: RuntimeState) -> Any:
        nonlocal outcome
        matches = _task_bindings(state, session_id, current_decision_id, task_id)
        if not matches:
            return None
        if len(matches) != 1:
            outcome = "ambiguous"
            return None

        (_decision_id, stage_key), (track, _binding) = next(iter(matches.items()))
        actual_role = (
            track.member_for_key(stage_key).role if track.member_for_key(stage_key) else stage_key
        )
        current_owner = track.member_tasks.get(stage_key) or track.stage_tasks.get(actual_role)
        if current_owner is None:
            current_owner = next(
                (
                    bound_id
                    for bound_id, bound_key in track.terminal_tasks.items()
                    if bound_key == stage_key
                ),
                None,
            )
        if current_owner is not None and current_owner != task_id:
            track.terminal_tasks.pop(task_id, None)
            track.updated_at = time.time()
            outcome = "recorded"
            return None
        stages: list[ExecutionStage] = []
        discharged = False
        for stage in track.stages:
            if stage.kind == "parallel_spawn":
                members = tuple(
                    replace(member, required=False)
                    if track.member_key(stage.stage_id, member.member_id) == stage_key
                    else member
                    for member in stage.members
                )
                if members != stage.members:
                    discharged = True
                    stage = replace(stage, members=members)
            elif stage.role == stage_key:
                discharged = True
                stage = replace(stage, required=False)
            stages.append(stage)
        if not discharged:
            return None

        actual_role = (
            track.member_for_key(stage_key).role if track.member_for_key(stage_key) else stage_key
        )
        track.stages = tuple(stages)
        track.stage_tasks.pop(actual_role, None)
        track.stage_tasks.pop(stage_key, None)
        track.member_tasks.pop(stage_key, None)
        track.terminal_tasks.pop(task_id, None)
        track.requested_at.pop(stage_key, None)
        track.not_found_streaks.pop(stage_key, None)
        if stage_key in track.requested:
            track.requested.remove(stage_key)
        marker = f"review_inconclusive:{stage_key}"
        if marker not in track.warnings:
            track.warnings.append(marker)
        track.updated_at = time.time()
        outcome = "recorded"
        return None

    _transaction(target, mutate)
    return outcome


def record_verify_retrieval_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    task_id: str,
    success: bool,
    failure_signal: FailureSignal | None = None,
) -> str:
    """Resolve a verifier retrieval against every bound track under one lock.

    A deterministic verifier is command-scoped: one task_id may legitimately
    bind the same step in multiple same-session debt tracks, and the terminal
    result fans out to every binding. Ambiguity protection is unnecessary here
    because each binding is derived from the exact verifier command.
    """
    if not session_id or not task_id:
        return "unmatched"
    target = Path(path) if path is not None else default_state_path()
    outcome = "unmatched"

    def mutate(state: RuntimeState) -> Any:
        nonlocal outcome
        ordered: list[tuple[str, ExecutionTrack]] = []
        current = state.get_execution(current_decision_id) if current_decision_id else None
        if current is not None and current.session_id == session_id:
            ordered.append((current.decision_id, current))
        ordered.extend(
            sorted(
                (
                    (decision_id, track)
                    for decision_id, track in state.executions.items()
                    if decision_id != current_decision_id and track.session_id == session_id
                ),
                key=lambda item: (item[1].updated_at, item[0]),
                reverse=True,
            )
        )
        matches = [
            (decision_id, track, step)
            for decision_id, track in ordered
            for bound_id, step in (
                list(track.verify_tasks.items()) + list(track.released_verify_tasks.items())
            )
            if bound_id == task_id
        ]
        if not matches:
            return None
        for _decision_id, track, step in matches:
            track.verify_tasks.pop(task_id, None)
            track.released_verify_tasks.pop(task_id, None)
            if success:
                if step in track.failed:
                    track.failed.remove(step)
                track.failure_reasons.pop(step, None)
                if step not in track.verified:
                    track.verified.append(step)
                track.stop_blocks = 0
            elif not success:
                if step in track.verified:
                    track.verified.remove(step)
                if step not in track.failed:
                    track.failed.append(step)
                track.failure_reasons[step] = (
                    failure_signal.reason
                    if failure_signal is not None
                    else "verifier result failure"
                )
            track.updated_at = time.time()
        outcome = "recorded"
        return None

    _transaction(target, mutate)
    return outcome


def record_session_debt_stage_tx(
    path: Path | str | None,
    session_id: str | None,
    excluded_decision_id: str | None,
    role: str,
    success: bool,
    window_seconds: float,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    consilium_after_failures: int = DEFAULT_CONSILIUM_THRESHOLD,
    failure_signal: FailureSignal | None = None,
) -> str | None:
    """Record an unambiguous role result against the newest matching debt."""
    if not session_id:
        return None
    target = Path(path) if path is not None else default_state_path()
    recorded_decision_id: str | None = None
    repair_log: dict[str, Any] | None = None
    repair_evidence: Any | None = None

    def mutate(state: RuntimeState) -> Any:
        nonlocal recorded_decision_id, repair_log, repair_evidence
        now = time.time()
        matches: list[tuple[float, str, ExecutionTrack, str]] = []
        for decision_id, track in state.executions.items():
            if (
                decision_id == excluded_decision_id
                or track.session_id != session_id
                or track.all_required_completed()
                or now - track.updated_at > window_seconds
            ):
                continue
            keys: list[str] = []
            for stage in track.stages:
                if not stage.required or not stage.spawnable:
                    continue
                if stage.kind == "parallel_spawn":
                    keys.extend(
                        track.member_key(stage.stage_id, member.member_id)
                        for member in track.incomplete_required_members(stage)
                        if member.role == role
                    )
                elif stage.role == role and role not in track.completed:
                    keys.append(role)
            if len(keys) == 1:
                matches.append((track.updated_at, decision_id, track, keys[0]))
        if not matches:
            return None
        _stamp, decision_id, track, key = max(matches, key=lambda item: (item[0], item[1]))
        actual_role = track.member_for_key(key).role if track.member_for_key(key) else key
        track.stage_tasks.pop(actual_role, None)
        track.requested_at.pop(key, None)
        repair_log, repair_evidence = _record_repair_result(
            track, key, success, consilium_after_failures
        )
        _apply_stage_result(
            state,
            track,
            key,
            success,
            actual_role=actual_role,
            decision_id=decision_id,
            subject=None,
            failure_signal=failure_signal,
            threshold=threshold,
            cooldown=cooldown,
            escalate=True,
            create_evidence=False,
            remove_completed_on_failure=True,
            clear_not_found_on_failure=True,
            reset_stop_blocks="success",
        )
        track.updated_at = time.time()
        recorded_decision_id = decision_id
        return None

    _transaction(target, mutate)
    _flush_repair_side_effects(repair_log, repair_evidence)
    return recorded_decision_id


def record_verify_fanout_tx(
    path: Path | str | None,
    session_id: str | None,
    current_decision_id: str | None,
    command: str | None,
    success: bool,
    window_seconds: float = 86400.0,
) -> int:
    """Record a foreground verifier result on every matching debt track."""
    if not session_id or not command or window_seconds < 0:
        return 0
    target = Path(path) if path is not None else default_state_path()
    recorded = 0

    def mutate(state: RuntimeState) -> Any:
        nonlocal recorded
        now = time.time()
        for decision_id, track in state.executions.items():
            if (
                decision_id == current_decision_id
                or track.session_id != session_id
                or now - track.updated_at > window_seconds
            ):
                continue
            step = track.next_verify_step()
            if not step:
                continue
            expected = next(
                (
                    stage.command
                    for stage in track.stages
                    if (stage.stage_id or stage.role) == step and not stage.spawnable
                ),
                "",
            )
            if not is_verifier_command(command, expected):
                continue
            if success and step not in track.verified:
                track.verified.append(step)
            elif not success and step in track.verified:
                track.verified.remove(step)
            track.updated_at = now
            recorded += 1
        return None

    _transaction(target, mutate)
    return recorded


def record_verify_tx(
    path: Path | str | None,
    decision_id: str,
    step: str,
    success: bool,
) -> None:
    """Mark a deterministic verification step complete (or keep it unverified).

    Verification steps are non-spawnable and tracked in ``ExecutionTrack.verified``,
    never in the spawn ``completed``/``failed`` lists and never through the role
    circuit breaker.
    """

    target = Path(path) if path is not None else default_state_path()

    def mutate(state: RuntimeState) -> Any:
        track = state.get_execution(decision_id)
        if track is None:
            return None
        if success:
            if step not in track.verified:
                track.verified.append(step)
            track.stop_blocks = 0
        else:
            if step in track.verified:
                track.verified.remove(step)
        track.updated_at = time.time()
        return None

    _transaction(target, mutate)
