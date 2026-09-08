from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import grokbuild.roles as roles
from grokbuild.availability import _endpoint_cache, _role_availability
from grokbuild.endpoint_resolution import latest_resolution_for_decision
from grokbuild.evidence import FailureSignal, classify_failure, is_quota_failure
from grokbuild.payloads import (
    classify_result_text,
    says_subagent,
    retrieval_result_status,
    retrieval_result_text,
    retrieval_section_role,
    retrieval_task_sections,
    retrieval_task_statuses,
    retrieval_transcript_text,
    spawn_result_status,
    spawn_tool_input,
    task_ids,
    terminal_background_ack,
    tool_result_raw,
)
from grokbuild.persist import atomic_update_json, parse_iso_utc
from grokbuild.policy import load_profiles
from grokbuild.shell_guard import _tool_command, is_readonly_shell
from grokbuild.state import RuntimeState, default_state_path, load_state, pending_child_bindings
from grokbuild.task_evidence import (
    MAX_ITEMS,
    EvidenceOutcome,
    Observation,
    TaskSpec,
    attach_task_spec,
    decide_completion,
    find_task_spec,
    observe,
    parse_task_result,
    record_task_evidence,
)
from grokbuild.transactions import (
    bind_debt_stage_task_tx,
    bind_debt_verify_task_tx,
    bind_linear_stage_task_tx,
    bind_parallel_member_task_tx,
    bind_terminal_task_tx,
    bind_verify_task_tx,
    record_retrieval_result_tx,
    record_review_inconclusive_tx,
    record_session_debt_stage_tx,
    record_stage_tx,
    record_verify_fanout_tx,
    record_verify_retrieval_tx,
    record_verify_tx,
    release_unresolvable_binding_tx,
    resolve_task_binding,
)
from grokbuild.transcript import (
    SPAWN_TOOLS,
    STATION_SKILLS,
    _canonical_role,
    _current_model,
    _iter_jsonl_reversed,
    _message_text,
    _session_dir,
    ensure_subagent_pin,
)
from grokbuild.verifiers import resolve_verifier

_DEFECT_MARKERS = re.compile(
    r"\b(?:defects?|bugs?|fails?|failed|failing|broken|regressions?|vulnerabilit(?:y|ies))\b"
    r"|\bdoes not pass\b|\bnot passing\b",
    re.IGNORECASE,
)
_INCONCLUSIVE_MARKERS = re.compile(
    r"\bcannot verify\b|\bcan not verify\b|\bunable to verify\b|\bno channel\b"
    r"|\bno access\b|\bverifier unavailable\b|\bno verifier\b|\bunreachable\b"
    r"|\bcannot inspect\b|\bunable to inspect\b|\bno tooling\b|\bcannot execute\b",
    re.IGNORECASE,
)


MAX_QUOTA_RESET_HORIZON_SECONDS = 7 * 86400


# Hook-owned callbacks are installed after hook.py defines its lifecycle helpers.
def _now() -> str:
    return datetime.now(UTC).isoformat()


READ_ONLY_ROLES = roles.EXECUTION_READ_ONLY_ROLES

# Callback placeholders are replaced by hook.py during normal hook startup.
_append_log = _circuit_params = _consilium_threshold = _enforceable_gate = None
_gate_active = _is_subagent = _is_verify_command = _load_execution = None
_next_required_spawn_role = _session_id = _stop_params = _verify_command_for = None
resolve_route = None


def configure(**callbacks: object) -> None:
    globals().update(callbacks)


def _child_transcript_is_terminal(task_id: str, history_path: Path) -> bool:
    """Require host/session completion evidence before trusting transcript prose."""
    folder = _session_dir(task_id)
    if folder is not None:
        summary = folder / "summary.json"
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key in ("status", "state", "session_state", "sessionStatus"):
                value = str(data.get(key) or "").casefold()
                if value in {"completed", "complete", "success", "succeeded", "finished", "terminated"}:
                    return True
        events = folder / "events.jsonl"
        if events.is_file():
            try:
                with events.open("r", encoding="utf-8") as handle:
                    last_nonblank = next(
                        (line for line in reversed(handle.readlines()) if line.strip()), None
                    )
                latest_event = json.loads(last_nonblank) if last_nonblank is not None else None
            except (OSError, json.JSONDecodeError, TypeError):
                latest_event = None
            if isinstance(latest_event, dict) and latest_event.get("type") == "turn_ended":
                return True
    for record in _iter_jsonl_reversed(history_path):
        if record.get("type") == "user":
            break
        if record.get("type") == "assistant" and (
            record.get("final") is True
            or record.get("terminal") is True
            or str(record.get("session_state") or "").casefold() in {"completed", "complete", "finished"}
        ):
            return True
    return False


def _last_child_assistant_text(task_id: str, history_path: Path | None = None) -> str:
    folder = _session_dir(task_id) if history_path is None else None
    history = (
        history_path
        if history_path is not None
        else (folder / "chat_history.jsonl" if folder else None)
    )
    if history is None:
        return ""
    for record in _iter_jsonl_reversed(history):
        if record.get("type") == "user":
            break
        if record.get("type") != "assistant" or record.get("synthetic_reason"):
            continue
        text = _message_text(record)
        if text.strip():
            return text
    return ""


def _classify_blocked_child(text: str, role: str) -> str:
    """Classify a leading-BLOCKED report, with defect markers taking precedence."""
    if _DEFECT_MARKERS.search(text):
        return "defect"
    canonical = _canonical_role(role)
    review_verdict = canonical in READ_ONLY_ROLES and (
        "review" in canonical
        or "verif" in canonical
        or "judge" in canonical
        or canonical in {"expert-rescue", "security"}
    )
    if review_verdict and _INCONCLUSIVE_MARKERS.search(text):
        return "review_inconclusive"
    return "defect"


def _pending_child_binding(session_id: str | None, task_id: str) -> tuple[str, str, str] | None:
    """Return ``(decision_id, stage_key, role)`` for a task bound to a tracked stage."""
    binding = resolve_task_binding(default_state_path(), session_id, None, task_id)
    if binding is None:
        return None
    decision_id, stage_key = binding
    track = load_state(default_state_path()).get_execution(decision_id)
    if track is None:
        return None
    member = track.member_for_key(stage_key)
    return decision_id, stage_key, _canonical_role(member.role if member is not None else stage_key)


def _pending_child_role(session_id: str | None, task_id: str) -> str:
    binding = _pending_child_binding(session_id, task_id)
    return binding[2] if binding is not None else ""


def _sweep_pending_children(session_id: str | None, route: dict) -> tuple[int, int, int, int, int]:
    task_ids = pending_child_bindings(load_state(default_state_path()), session_id)
    blocked = [
        (task_id, text)
        for task_id in task_ids
        if re.match(
            r"^#{0,3}\s*BLOCKED\b",
            (text := _last_child_assistant_text(task_id).strip()),
        )
    ]
    threshold, cooldown = _circuit_params(route)
    consilium_after_failures = _consilium_threshold(route)
    recorded = 0
    defect = 0
    inconclusive = 0
    for task_id, text in blocked:
        role = _pending_child_role(session_id, task_id)
        classification = _classify_blocked_child(text, role)
        blocked_signal = FailureSignal(text=text[:2000], reason="child blocked")
        if role and _quota_signal(blocked_signal):
            _open_quota_circuit(
                blocked_signal,
                route,
                role,
                cooldown,
                model_binding=_current_model(task_id),
                exact_binding=True,
            )
        if classification == "review_inconclusive":
            outcome = record_review_inconclusive_tx(default_state_path(), session_id, None, task_id)
            inconclusive += outcome == "recorded"
        else:
            outcome = record_retrieval_result_tx(
                default_state_path(),
                session_id,
                None,
                task_id,
                success=False,
                threshold=threshold,
                cooldown=cooldown,
                consilium_after_failures=consilium_after_failures,
                escalate=False,
            )
            defect += outcome == "recorded"
        recorded += outcome == "recorded"
    return len(task_ids), len(blocked), recorded, defect, inconclusive


def _terminal_stage_key(track: object) -> str | None:
    requested = set(getattr(track, "requested", ()))
    completed = set(getattr(track, "completed", ()))
    implementation_keys: list[str] = []
    stage_keys: set[str] = set()
    for stage in getattr(track, "stages", ()):
        if not getattr(stage, "spawnable", False):
            continue
        if stage.kind == "parallel_spawn":
            entries = (
                (track.member_key(stage.stage_id, member.member_id), member.role)
                for member in stage.members
            )
        else:
            entries = ((stage.role, stage.role),)
        for key, role in entries:
            stage_keys.add(key)
            if key in requested | completed and role in STATION_SKILLS["implement"]:
                implementation_keys.append(key)
    if implementation_keys:
        return implementation_keys[-1]
    return next(
        (key for key in reversed(getattr(track, "requested", ())) if key in stage_keys), None
    )


def _owed_result_key(track: object, role: str) -> str | None:
    keys: list[str] = []
    for stage in getattr(track, "stages", ()):
        if not stage.required or not stage.spawnable:
            continue
        if stage.kind == "parallel_spawn":
            keys.extend(
                track.member_key(stage.stage_id, member.member_id)
                for member in track.incomplete_required_members(stage)
                if member.role == role
            )
        elif (
            stage.role == role
            and role not in track.completed
            and not track.review_inconclusive(role)
        ):
            keys.append(role)
    requested_order = list(getattr(track, "requested", ()))
    requested = [key for key in keys if key in requested_order]
    if requested:
        requested_index = {key: index for index, key in enumerate(requested_order)}
        requested_at = getattr(track, "requested_at", {})
        # Spawn order is the deterministic fallback for legacy members without stamps.
        return min(
            requested,
            key=lambda key: (
                0 if key in requested_at else 1,
                requested_at.get(key, 0.0),
                requested_index[key],
            ),
        )
    return keys[0] if len(keys) == 1 else None


def _member_result_key(track: object, role: str, require_unbound_task: bool = False) -> str | None:
    for stage in getattr(track, "stages", ()):
        if stage.kind != "parallel_spawn":
            continue
        for member in stage.members:
            key = track.member_key(stage.stage_id, member.member_id)
            if (
                member.role == role
                and key in track.requested
                and key not in track.completed
                and (not require_unbound_task or key not in track.member_tasks)
            ):
                return key
    return None


def _retrieval_transcript_text(data: dict) -> str | None:
    return retrieval_transcript_text(data, _session_dir, _session_id, _iter_jsonl_reversed)


def _retrieval_task_statuses(data: dict, requested_ids: list[str] | None = None) -> dict[str, str]:
    return retrieval_task_statuses(data, _retrieval_transcript_text, requested_ids=requested_ids)


def _retrieval_result_status(data: dict, requested_ids: list[str] | None = None) -> str:
    return retrieval_result_status(data, _retrieval_transcript_text, requested_ids=requested_ids)


def _attach_stage_spec(
    *,
    route: dict,
    track: object | None,
    decision_id: str | None,
    session_id: str | None,
    stage_key: str,
    role: str,
) -> None:
    """Fix what "done" means for this stage attempt before the child starts.

    Without a contract manifest the runtime can only state the identity of the
    attempt: which decision, which stage, which role. That is still enough for
    the strict rule to bite - the result must be typed, must be for this stage,
    must claim success with nothing unresolved, and its declared changes must
    actually be present in the worktree. Route-level deterministic checks stay
    where they already live, in the separate ``verified`` track.

    Attaching before the spawn is what makes the contract citable afterwards:
    the first attachment for a stage wins, so a later attempt cannot widen it.
    """
    if not decision_id or not stage_key:
        return
    try:
        attach_task_spec(
            TaskSpec(
                decision_id=decision_id,
                stage_key=stage_key,
                role=role,
                session_id=session_id,
            )
        )
    except OSError:
        pass


def _evidence_policy(route: dict) -> str | None:
    """Return the route profile's evidence policy, or None for the legacy rule."""
    try:
        profile = load_profiles().get(str(route.get("profile") or "default"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return getattr(profile, "evidence_policy", None) if profile is not None else None


def _observed_changed_paths(root: str | None) -> tuple[str, ...]:
    """Return the worktree paths the runtime itself sees as changed.

    The child's declaration is a claim; this is the runtime's own reading of
    the repository, and the two are compared rather than trusted. An unreadable
    worktree yields no observation at all, which fails the comparison closed.
    """
    if not root:
        return ()
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    paths: list[str] = []
    for line in completed.stdout.splitlines()[:MAX_ITEMS]:
        entry = line[3:].strip() if len(line) > 3 else ""
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip('"')
        if entry:
            paths.append(entry)
    return tuple(paths)


def _evidence_seam(
    *,
    data: dict,
    route: dict,
    session_id: str | None,
    decision_id: str | None,
    task_id: str,
    legacy_success: bool,
    text: str | None,
    resolved_binding: tuple[str, str] | None = None,
) -> bool:
    """Record typed evidence for one retrieval and return the completion decision.

    Under the legacy policy this only observes: it records whether the child
    supplied a typed result and returns today's answer unchanged. Under the
    strict policy the runtime verifies the typed result against the spec that
    was attached before the spawn, and only a verified result completes a stage.
    """
    policy = _evidence_policy(route)
    binding = (
        resolved_binding
        or resolve_task_binding(default_state_path(), session_id, decision_id, task_id)
    )
    if binding is None:
        return legacy_success
    bound_decision, stage_key = binding
    spec = find_task_spec(bound_decision, stage_key)
    result = parse_task_result(text)
    observation = Observation()
    if spec is not None and (policy or "").strip().casefold() == "strict":
        track = _load_execution(bound_decision)
        observation = observe(
            spec,
            repo_root=data.get("workspaceRoot") or data.get("cwd") or ".",
            changed_paths=_observed_changed_paths(data.get("workspaceRoot") or data.get("cwd")),
            checks={step: True for step in (track.verified if track else ())},
        )
    decision = decide_completion(
        policy=policy,
        legacy_success=legacy_success,
        spec=spec,
        result=result,
        observation=observation,
    )
    try:
        record_task_evidence(
            EvidenceOutcome(
                decision_id=bound_decision,
                session_id=session_id,
                stage_key=stage_key,
                task_id=task_id,
                policy=decision.policy,
                typed_result=decision.typed_result,
                verification=decision.verification,
            )
        )
    except OSError:
        pass
    return decision.completes


def _failure_signal(data: dict, reason: str = "tool result failure") -> FailureSignal:
    raw = tool_result_raw(data)
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    status = raw.get("status") if isinstance(raw, dict) else None
    return FailureSignal(text=text[:2000], reason=reason, status=status)


def _spawn_model(data: dict) -> str | None:
    model = _spawn_field(data, "model", None)
    return str(model) if model else None


def _quota_reset_deadline(role_name: str, provider: str, now: float) -> float | None:
    registry = roles.load_registry()
    role = registry.get(role_name)
    meta = registry.provider_catalog.get(role.model or "") if role else None
    keys = {
        endpoint.availability_key or f"{role.model}@{endpoint.provider}"
        for endpoint in (meta.endpoints if meta else ())
        if endpoint.provider == provider
    }
    keys.add(provider)
    deadlines: list[float] = []
    cache = _endpoint_cache()
    for key in keys:
        entry = cache.get(key)
        quota = entry.get("quota") if isinstance(entry, Mapping) else None
        windows = quota.get("windows") if isinstance(quota, Mapping) else None
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, Mapping):
                continue
            used = window.get("used_percent")
            reset = window.get("resets_at")
            if not isinstance(used, (int, float)) or float(used) < 90.0:
                continue
            if isinstance(reset, (int, float)):
                deadline = float(reset)
            elif isinstance(reset, str):
                try:
                    deadline = parse_iso_utc(reset).timestamp()
                except ValueError:
                    continue
            else:
                continue
            if now < deadline <= now + MAX_QUOTA_RESET_HORIZON_SECONDS:
                deadlines.append(deadline)
    return min(deadlines) if deadlines else None


def _quota_signal(signal: FailureSignal) -> bool:
    """True when a failure signal is a model-class quota/429 rejection."""
    return classify_failure(signal) == "model" and is_quota_failure(signal.detail())


def _failed_endpoint_provider(
    role_name: str, decision_id: object, model_binding: str | None, *, exact_binding: bool
) -> str:
    """Name the provider that served a failed child of ``role_name``.

    An endpoint binding named by the caller wins when it is exact (the child's
    own session records the model id it ran under) or when it names a
    non-default endpoint. The default model id is ambiguous — a spawn input may
    predate the hook's rebind — so the decision's persisted resolution is
    consulted before the declared primary endpoint, then the role provider.
    """
    registry = roles.load_registry()
    role = registry.get(role_name)
    if role is None:
        return ""
    meta = registry.provider_catalog.get(role.model or "")
    endpoints = meta.endpoints if meta and meta.explicit_endpoints else ()
    bound = next(
        (item for item in endpoints if model_binding and item.model_binding == model_binding),
        None,
    )
    if bound is not None and (exact_binding or model_binding != role.model):
        return bound.provider
    resolution = (
        latest_resolution_for_decision(str(decision_id), model=role.model)
        if decision_id and role.model
        else None
    )
    if resolution is not None and resolution.provider:
        return resolution.provider
    if bound is not None:
        return bound.provider
    if endpoints:
        return endpoints[0].provider
    return role.provider or ""


def _open_quota_circuit(
    signal: FailureSignal,
    route: Mapping[str, object],
    role_name: str,
    cooldown: int,
    *,
    model_binding: str | None = None,
    exact_binding: bool = False,
) -> dict[str, object] | None:
    """Hold the provider behind a quota/429 failure until its quota resets."""
    if not _quota_signal(signal):
        return None
    provider = _failed_endpoint_provider(
        role_name, route.get("decision_id"), model_binding, exact_binding=exact_binding
    )
    if not provider:
        return None
    now = time.time()
    deadline = _quota_reset_deadline(role_name, provider, now) or now + cooldown

    def update(raw: object) -> dict[str, object]:
        state = (
            RuntimeState.from_dict(raw)
            if isinstance(raw, Mapping)
            else RuntimeState(source_path=default_state_path())
        )
        state.set_provider_unavailable(provider, deadline, "quota/429", now=now)
        return state.to_dict()

    atomic_update_json(
        default_state_path(),
        update,
        default=RuntimeState(source_path=default_state_path()).to_dict(),
    )
    runtime_state = route.get("_runtime_state")
    if isinstance(runtime_state, RuntimeState):
        runtime_state.set_provider_unavailable(provider, deadline, "quota/429", now=now)
    return {"provider": provider, "unavailable_until": deadline, "reason": "quota/429"}


def _maybe_open_quota_circuit(
    data: dict, route: Mapping[str, object], role_name: str, cooldown: int
) -> dict[str, object] | None:
    """Open the quota circuit from a synchronous spawn result or spawn failure."""
    return _open_quota_circuit(
        _failure_signal(data, "child result failure"),
        route,
        role_name,
        cooldown,
        model_binding=_spawn_model(data),
    )


def _retrieval_quota_circuits(
    data: dict,
    route: Mapping[str, object],
    session_id: str | None,
    statuses: Mapping[str, str],
    cooldown: int,
) -> dict[str, object] | None:
    """Open the provider circuit for each failed retrieval section citing a quota rejection.

    Background children die out of band: their 429 arrives inside the section
    ``get_command_or_subagent_output`` returns for them, not through a spawn
    result. The section names the child's role, and the child's own session
    records the model id it ran under, so attribution needs no stage binding —
    an ad-hoc spawn after the pipeline finished still holds the provider.

    A child bound to a tracked stage additionally gets the reactive-failover
    marker for that stage, so the reopened stage's recipe names the reserve.
    An unbound child has no stage to reopen: the hold alone, together with the
    ungated rebind, steers its next spawn.
    """
    failed = [task_id for task_id, status in statuses.items() if status == "failure"]
    if not failed:
        return None
    text = retrieval_result_text(data, _retrieval_transcript_text) or ""
    sections = retrieval_task_sections(text)
    opened: dict[str, object] | None = None
    for task_id in failed:
        section = sections.get(task_id) or (text if len(statuses) == 1 else "")
        signal = FailureSignal(text=section[:2000], reason="child retrieval failure")
        if not _quota_signal(signal):
            continue
        binding = _pending_child_binding(session_id, task_id)
        role = binding[2] if binding is not None else ""
        if not role:
            named = retrieval_section_role(section)
            role = _canonical_role(named) if named else ""
        if not role:
            continue
        circuit = _open_quota_circuit(
            signal,
            route,
            role,
            cooldown,
            model_binding=_current_model(task_id),
            exact_binding=True,
        )
        if circuit is None:
            continue
        opened = {**circuit, "task_id": task_id, "role": role, "path": "retrieval"}
        if binding is not None:
            bound_decision, stage_key, _role = binding
            reactive = _reactive_failover(
                data, {**route, "decision_id": bound_decision}, role, stage_key, circuit
            )
            if reactive is not None:
                opened["reactive_failover"] = reactive
    return opened


def _update_reactive_marker(
    raw: object, route: Mapping[str, object], key: str, marker: dict
) -> dict[str, object]:
    current = RuntimeState.from_dict(raw) if isinstance(raw, Mapping) else RuntimeState()
    current_track = current.get_execution(str(route.get("decision_id") or ""))
    if (
        current_track is not None
        and int(current_track.reactive_respawns.get(key, {}).get("count", 0)) >= 1
    ):
        current_track.reactive_respawns[key] = marker
        current_track.updated_at = time.time()
    return current.to_dict()


def _reactive_failover(
    data: dict,
    route: Mapping[str, object],
    role_name: str,
    key: str | None,
    circuit: dict[str, object] | None,
) -> dict[str, object] | None:
    """Record one reserve retry when the quota circuit leaves one usable endpoint."""
    if circuit is None or not key:
        return None
    state = route.get("_runtime_state")
    if not isinstance(state, RuntimeState):
        state = load_state()
    track = state.get_execution(str(route.get("decision_id") or ""))
    if track is None:
        return None
    existing = track.reactive_respawns.get(key, {})
    if int(existing.get("count", 0)) >= 1:
        if circuit.get("provider") == existing.get("to_provider") and circuit.get(
            "unavailable_until"
        ):
            marker = dict(existing)
            marker["from_provider"] = existing.get("from_provider", "")
            marker["to_provider"] = existing.get("to_provider", "")
            marker["unavailable_until"] = float(circuit["unavailable_until"])
            track.reactive_respawns[key] = marker
            track.updated_at = time.time()
            atomic_update_json(
                default_state_path(),
                lambda raw: _update_reactive_marker(raw, route, key, marker),
                default=state.to_dict(),
            )
        return None
    registry = roles.load_registry()
    endpoint_events: list[dict[str, object]] = []
    ok, _signal = _role_availability(
        state,
        role_name,
        registry,
        stale=False,
        session_id=str(route.get("session_id") or "") or None,
        endpoint_events=endpoint_events,
    )
    model_event = endpoint_events[-1] if endpoint_events else None
    to_provider = model_event.get("provider") if ok and model_event else None
    from_provider = str(circuit.get("provider") or "")
    if not isinstance(to_provider, str) or not to_provider or to_provider == from_provider:
        return None
    marker = {
        "count": 1,
        "from_provider": from_provider,
        "to_provider": to_provider,
        "role": role_name,
        "unavailable_until": float(circuit["unavailable_until"]),
    }
    persisted = False

    def update(raw: object) -> dict[str, object]:
        nonlocal persisted
        persisted = False
        current = RuntimeState.from_dict(raw) if isinstance(raw, Mapping) else RuntimeState()
        current_track = current.get_execution(str(route.get("decision_id") or ""))
        if (
            current_track is not None
            and int(current_track.reactive_respawns.get(key, {}).get("count", 0)) < 1
        ):
            current_track.reactive_respawns[key] = marker
            current_track.updated_at = time.time()
            persisted = True
        return current.to_dict()

    atomic_update_json(default_state_path(), update, default=state.to_dict())
    if persisted:
        track.reactive_respawns[key] = marker
    event = {
        "observed_at": _now(),
        "event": "reactive_failover",
        "session_id": route.get("session_id"),
        "decision_id": route.get("decision_id"),
        "from_provider": from_provider,
        "to_provider": to_provider,
        "role": role_name,
    }
    _append_log(event, state=state)
    return {"from_provider": from_provider, "to_provider": to_provider, "role": role_name}


def handle_post_tool(data: dict, spec: dict) -> None:
    """Observe-only: record received spawn results and edit outcomes.

    In-progress/background acknowledgements are neither success nor failure:
    they are ignored so they cannot complete a stage, feed the circuit breaker,
    or reset it.
    """

    session_id = _session_id(data)
    payload_sub = says_subagent(data)
    if payload_sub:
        ensure_subagent_pin(session_id)
    is_subagent_session = payload_sub or _is_subagent(session_id)
    tool = str(data.get("toolName") or data.get("tool_name") or "").casefold()
    route = resolve_route(
        session_id,
        spec,
        persist=False,
        event="post_tool_use",
        extra={"tool": tool},
        workspace_root=data.get("workspaceRoot") or data.get("cwd"),
    )
    decision_id = route.get("decision_id")
    mode = route.get("mode") or "dynamic"
    threshold, cooldown = _circuit_params(route)
    consilium_after_failures = _consilium_threshold(route)
    retrieval_telemetry: dict | None = None
    quota_circuit: dict[str, object] | None = None
    if mode != "static" and tool in SPAWN_TOOLS and decision_id:
        role = _canonical_role(_spawn_subagent_type(data))
        status = spawn_result_status(data)
        result_text = retrieval_result_text(data, _retrieval_transcript_text)

        def settled_success(task_id: str, binding: tuple[str, str] | None = None) -> bool:
            return _evidence_seam(
                data=data,
                route=route,
                session_id=session_id,
                decision_id=decision_id,
                task_id=task_id,
                legacy_success=(status == "success"),
                text=result_text,
                resolved_binding=binding,
            )

        if role and status == "failure":
            quota_circuit = _maybe_open_quota_circuit(data, route, role, cooldown)
            track = _load_execution(decision_id)
            reactive_key = (_member_result_key(track, role) if track else None) or (
                _owed_result_key(track, role) if track else None
            )
            _reactive_failover(data, route, role, reactive_key, quota_circuit)
        if role and status == "incomplete":
            ids = task_ids(data, include_result=True, include_free_text_task_ids=False)
            if ids:
                bound = bind_parallel_member_task_tx(
                    default_state_path(), decision_id, role, ids[0]
                )
                if bound is None:
                    bound = bind_linear_stage_task_tx(
                        default_state_path(), decision_id, role, ids[0]
                    )
                if bound is None and not is_subagent_session:
                    _limit, debt_window = _stop_params(route)
                    bind_debt_stage_task_tx(
                        default_state_path(),
                        session_id,
                        decision_id,
                        role,
                        ids[0],
                        debt_window,
                    )
        elif role:
            ids = task_ids(data, include_result=True, include_free_text_task_ids=False)
            recorded = False
            for task_id in ids:
                bound = bind_parallel_member_task_tx(
                    default_state_path(), decision_id, role, task_id
                )
                if bound is None:
                    bound = bind_linear_stage_task_tx(
                        default_state_path(), decision_id, role, task_id
                    )
                if bound is not None:
                    record_retrieval_result_tx(
                        default_state_path(),
                        session_id,
                        decision_id,
                        task_id,
                        success=settled_success(task_id),
                        threshold=threshold,
                        cooldown=cooldown,
                        consilium_after_failures=consilium_after_failures,
                        failure_signal=_failure_signal(data),
                    )
                    recorded = True
                    break
                resolved_binding = resolve_task_binding(
                    default_state_path(), session_id, decision_id, task_id
                )
                if resolved_binding is None:
                    continue
                outcome = record_retrieval_result_tx(
                    default_state_path(),
                    session_id,
                    decision_id,
                    task_id,
                    success=settled_success(task_id, resolved_binding),
                    threshold=threshold,
                    cooldown=cooldown,
                    consilium_after_failures=consilium_after_failures,
                    failure_signal=_failure_signal(data),
                )
                if outcome == "recorded":
                    recorded = True
                    break
            if not recorded and ids and not is_subagent_session:
                _limit, debt_window = _stop_params(route)
                if (
                    bind_debt_stage_task_tx(
                        default_state_path(),
                        session_id,
                        decision_id,
                        role,
                        ids[0],
                        debt_window,
                    )
                    is not None
                ):
                    record_retrieval_result_tx(
                        default_state_path(),
                        session_id,
                        decision_id,
                        ids[0],
                        success=settled_success(ids[0]),
                        threshold=threshold,
                        cooldown=cooldown,
                        consilium_after_failures=consilium_after_failures,
                        failure_signal=_failure_signal(data),
                    )
            elif not ids:
                track = _load_execution(decision_id)
                key = _owed_result_key(track, role) if track else None
                if key is not None:
                    record_stage_tx(
                        default_state_path(),
                        decision_id,
                        "result",
                        role=key,
                        success=settled_success("", (decision_id, key)),
                        threshold=threshold,
                        cooldown=cooldown,
                        consilium_after_failures=consilium_after_failures,
                        failure_signal=_failure_signal(data),
                    )
                else:
                    _limit, debt_window = _stop_params(route)
                    record_session_debt_stage_tx(
                        default_state_path(),
                        session_id,
                        decision_id,
                        role,
                        success=settled_success("", (decision_id, role)),
                        window_seconds=debt_window,
                        threshold=threshold,
                        cooldown=cooldown,
                        consilium_after_failures=consilium_after_failures,
                        failure_signal=_failure_signal(data),
                    )
    if (
        mode != "static"
        and tool == "run_terminal_command"
        and decision_id
        and terminal_background_ack(data)
        and not is_readonly_shell(_tool_command(data))
    ):
        track = _load_execution(decision_id)
        verify_bound: set[str] = set()
        next_verify = track.next_verify_step() if track else None
        command = _tool_command(data)
        requested_ids = task_ids(data, include_result=True)
        if (
            _gate_active(route, spec)
            and next_verify
            and track is not None
            and _next_required_spawn_role(track) is None
            and _is_verify_command(command, _verify_command_for(track, next_verify))
        ):
            for task_id in requested_ids:
                if bind_verify_task_tx(default_state_path(), decision_id, task_id, next_verify):
                    verify_bound.add(task_id)
        elif (
            requested_ids
            and not is_subagent_session
            and any(
                _is_verify_command(command, expected)
                for expected in (
                    resolve_verifier(data.get("workspaceRoot") or data.get("cwd"), spec) or ()
                )
            )
        ):
            _limit, debt_window = _stop_params(route)
            for task_id in requested_ids:
                if bind_debt_verify_task_tx(
                    default_state_path(),
                    session_id,
                    decision_id,
                    command,
                    task_id,
                    debt_window,
                ):
                    verify_bound.add(task_id)
        stage_key = _terminal_stage_key(track) if track else None
        if stage_key:
            for task_id in requested_ids:
                if task_id not in verify_bound:
                    bind_terminal_task_tx(default_state_path(), decision_id, task_id, stage_key)
    if tool == "get_command_or_subagent_output":
        ids = task_ids(data)
        aggregate_status = _retrieval_result_status(data, requested_ids=ids)
        task_statuses = _retrieval_task_statuses(data, requested_ids=ids)
        reported_statuses = {
            task_id: task_statuses[task_id] for task_id in ids if task_id in task_statuses
        }
        if len(ids) == 1 and not task_statuses:
            reported_statuses[ids[0]] = aggregate_status
        retrieval_telemetry = {"ids": len(ids), "statuses": reported_statuses}
        if (
            len(ids) > 1
            and len(reported_statuses) == len(ids)
            and all(status == "incomplete" for status in reported_statuses.values())
        ):
            retrieval_telemetry["no_op"] = "no-op, composition unchanged"
        if mode != "static":
            # Bindings are consumed by the settlement below, so attribute first.
            quota_circuit = (
                _retrieval_quota_circuits(data, route, session_id, reported_statuses, cooldown)
                or quota_circuit
            )
        if mode != "static" and decision_id and not is_subagent_session:
            outcomes = {}
            for task_id in ids:
                status = task_statuses.get(task_id)
                if status is None:
                    if len(ids) == 1 and not task_statuses:
                        status = aggregate_status
                    else:
                        continue
                if status in {"incomplete", "not_found"}:
                    if task_statuses.get(task_id) != "not_found":
                        continue
                    if status == "not_found":
                        child_dir = _session_dir(task_id)
                        history = child_dir / "chat_history.jsonl" if child_dir else None
                        fallback_status: str | None = None
                        if history is not None:
                            try:
                                before = history.stat()
                                text = _last_child_assistant_text(task_id, history)
                                after = history.stat()
                                stable = (
                                    before.st_dev == after.st_dev
                                    and before.st_ino == after.st_ino
                                    and before.st_size == after.st_size
                                    and before.st_mtime_ns == after.st_mtime_ns
                                )
                                if stable and text.strip():
                                    if re.match(r"^#{0,3}\s*BLOCKED\b", text.strip()):
                                        fallback_status = "failure"
                                    elif _child_transcript_is_terminal(task_id, history):
                                        fallback_status = classify_result_text(text)
                            except OSError:
                                fallback_status = None
                        if fallback_status in {"success", "failure"}:
                            outcomes[task_id] = record_retrieval_result_tx(
                                default_state_path(),
                                session_id,
                                decision_id,
                                task_id,
                                success=(
                                    _evidence_seam(
                                        data=data,
                                        route=route,
                                        session_id=session_id,
                                        decision_id=decision_id,
                                        task_id=task_id,
                                        legacy_success=(fallback_status == "success"),
                                        text=text,
                                    )
                                    if fallback_status == "success"
                                    else False
                                ),
                                threshold=threshold,
                                cooldown=cooldown,
                                consilium_after_failures=consilium_after_failures,
                                failure_signal=_failure_signal(data),
                            )
                            continue
                        outcomes[task_id] = release_unresolvable_binding_tx(
                            default_state_path(),
                            session_id,
                            decision_id,
                            task_id,
                        )
                    continue
                outcomes[task_id] = record_retrieval_result_tx(
                    default_state_path(),
                    session_id,
                    decision_id,
                    task_id,
                    success=_evidence_seam(
                        data=data,
                        route=route,
                        session_id=session_id,
                        decision_id=decision_id,
                        task_id=task_id,
                        legacy_success=(status == "success"),
                        text=retrieval_result_text(data, _retrieval_transcript_text),
                    ),
                    threshold=threshold,
                    cooldown=cooldown,
                    consilium_after_failures=consilium_after_failures,
                    failure_signal=_failure_signal(data),
                )
            if outcomes:
                retrieval_telemetry["outcomes"] = outcomes
            verify_outcomes = {}
            for task_id, status in reported_statuses.items():
                if status in {"incomplete", "not_found"}:
                    continue
                verify_outcomes[task_id] = record_verify_retrieval_tx(
                    default_state_path(),
                    session_id,
                    decision_id,
                    task_id,
                    success=(status == "success"),
                    failure_signal=_failure_signal(data),
                )
            if verify_outcomes:
                retrieval_telemetry["verify_outcomes"] = verify_outcomes
    if (
        mode != "static"
        and tool == "run_terminal_command"
        and decision_id
        and not terminal_background_ack(data)
    ):
        track = _load_execution(decision_id)
        next_verify = track.next_verify_step() if track else None
        success = _verify_result_status(data) == "success"
        if (
            next_verify
            and _next_required_spawn_role(track) is None
            and _is_verify_command(_tool_command(data), _verify_command_for(track, next_verify))
        ):
            record_verify_tx(default_state_path(), decision_id, next_verify, success)
        # Debt fan-out is workspace-scoped, not current-track-scoped: a turn with
        # no due verify step of its own still clears same-session debt.
        if not is_subagent_session and any(
            _is_verify_command(_tool_command(data), expected)
            for expected in resolve_verifier(data.get("workspaceRoot") or data.get("cwd"), spec)
            or ()
        ):
            _limit, debt_window = _stop_params(route)
            record_verify_fanout_tx(
                default_state_path(),
                session_id,
                decision_id,
                _tool_command(data),
                success,
                debt_window,
            )
    log_record = {
        "observed_at": _now(),
        "event": "post_tool_use",
        "session_id": session_id,
        "tool": tool,
        "intent": route.get("intent"),
        "required_model": route.get("required_model"),
        "current_model": route.get("current_model"),
        "has_strong": route.get("has_strong"),
        "source": route.get("source"),
    }
    if tool == "get_command_or_subagent_output":
        log_record["decision_id"] = decision_id
        log_record["retrieval"] = retrieval_telemetry or {"ids": 0, "statuses": {}}
    if quota_circuit is not None:
        log_record["quota_circuit"] = quota_circuit
    _append_log(log_record, state=route.get("_runtime_state"))


def handle_post_tool_failure(data: dict, spec: dict) -> None:
    """Observe-only: record a spawn failure; it never completes a stage and
    feeds the role circuit breaker (profile threshold/cooldown)."""

    session_id = _session_id(data)
    payload_sub = says_subagent(data)
    if payload_sub:
        ensure_subagent_pin(session_id)
    is_subagent_session = payload_sub or _is_subagent(session_id)
    tool = str(data.get("toolName") or data.get("tool_name") or "").casefold()
    route = resolve_route(
        session_id,
        spec,
        persist=False,
        event="post_tool_use_failure",
        extra={"tool": tool},
        workspace_root=data.get("workspaceRoot") or data.get("cwd"),
    )
    decision_id = route.get("decision_id")
    mode = route.get("mode") or "dynamic"
    threshold, cooldown = _circuit_params(route)
    consilium_after_failures = _consilium_threshold(route)
    quota_circuit: dict[str, object] | None = None
    if mode != "static" and tool in SPAWN_TOOLS and decision_id:
        role = _canonical_role(_spawn_subagent_type(data))
        track = _load_execution(decision_id)
        key = (_member_result_key(track, role) if track else None) or (
            _owed_result_key(track, role) if track else None
        )
        if role:
            quota_circuit = _maybe_open_quota_circuit(data, route, role, cooldown)
            _reactive_failover(data, route, role, key, quota_circuit)
            record_stage_tx(
                default_state_path(),
                decision_id,
                "result",
                role=key or role,
                success=False,
                threshold=threshold,
                cooldown=cooldown,
                consilium_after_failures=consilium_after_failures,
                failure_signal=_failure_signal(data, "post tool failure"),
            )
    retrieval_telemetry: dict | None = None
    if tool == "get_command_or_subagent_output":
        ids = task_ids(data)
        outcomes: dict[str, str] = {}
        if mode != "static" and decision_id and not is_subagent_session:
            for task_id in ids:
                outcomes[task_id] = record_retrieval_result_tx(
                    default_state_path(),
                    session_id,
                    decision_id,
                    task_id,
                    success=False,
                    threshold=threshold,
                    cooldown=cooldown,
                    consilium_after_failures=consilium_after_failures,
                    failure_signal=_failure_signal(data, "post tool failure"),
                )
        retrieval_telemetry = {"ids": len(ids), "outcomes": outcomes}
    log_record = {
        "observed_at": _now(),
        "event": "post_tool_use_failure",
        "session_id": session_id,
        "tool": tool,
        "intent": route.get("intent"),
        "source": route.get("source"),
    }
    if retrieval_telemetry is not None:
        log_record["retrieval"] = retrieval_telemetry
    if quota_circuit is not None:
        log_record["quota_circuit"] = quota_circuit
    _append_log(log_record, state=route.get("_runtime_state"))


def _handle_failure(data: dict, spec: dict, event: str) -> int:
    """Fail closed only when the failed lifecycle handler was gated."""
    try:
        route = resolve_route(
            _session_id(data),
            spec,
            persist=False,
            event=event,
            workspace_root=data.get("workspaceRoot") or data.get("cwd"),
        )
        gated = _gate_active(route, spec)
    except Exception:
        gated = True
    if gated:
        print(json.dumps({"decision": "deny", "reason": "hook internal error; failing closed"}))
        return 2
    print(json.dumps({"decision": "allow"}))
    return 0


def _spawn_field(data: dict, key: str, default: object = "") -> object:
    return spawn_tool_input(data).get(key, default)


def _spawn_subagent_type(data: dict) -> str:
    value = _spawn_field(data, "subagent_type") or _spawn_field(data, "subagentType")
    return str(value or "").casefold()


def _verify_result_status(data: dict) -> str:
    """Classify a deterministic verifier result without spawn-result heuristics."""
    raw = tool_result_raw(data)

    def exit_code(value: object) -> int | None:
        if isinstance(value, dict):
            for key in ("exit_code", "exitCode", "status"):
                code = value.get(key)
                if isinstance(code, int) and not isinstance(code, bool):
                    return code
                if isinstance(code, str):
                    try:
                        return int(code.strip())
                    except ValueError:
                        pass
            for nested in value.values():
                code = exit_code(nested)
                if code is not None:
                    return code
        if isinstance(value, (list, tuple)):
            for nested in value:
                code = exit_code(nested)
                if code is not None:
                    return code
        return None

    if isinstance(raw, dict):
        # Verifiers intentionally mirror the spawn-path status precedence.
        status = raw.get("status")
        if isinstance(status, str):
            normalized = status.strip().casefold()
            if normalized in {"failed", "error", "failure"}:
                return "failure"
            if normalized in {"success", "ok", "passed"}:
                return "success"
        for key in ("exit_code", "exitCode"):
            code = exit_code({key: raw.get(key)})
            if code is not None:
                return "success" if code == 0 else "failure"
    elif isinstance(raw, str):
        header = re.search(
            r"^\s*(?:Exit Code|exit|Exit):?\s*(-?\d+)\s*$",
            raw,
            re.IGNORECASE | re.MULTILINE,
        )
        if header:
            return "success" if int(header.group(1)) == 0 else "failure"
    code = exit_code(raw)
    if code is not None:
        return "success" if code == 0 else "failure"
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, dict):
        text = "\n".join(
            raw[key]
            for key in ("stdout", "output", "content", "text", "result")
            if isinstance(raw.get(key), str)
        )
    else:
        return "incomplete"
    if not text.strip():
        return "incomplete"
    explicit_failure = re.compile(
        r"(?:\b[1-9]\d*\s+fail(?:ed|ures?)\b|"
        r"\bfailed\s*:\s*[1-9]\d*\b|"
        r"\b[1-9]\d*\s+errors?\b|"
        r"\berrors?\s*:\s*[1-9]\d*\b|"
        r"\bTraceback\s*\(most recent call last\)\s*:?)",
        re.IGNORECASE,
    )
    if explicit_failure.search(text) or re.search(r"\bFAILED\b", text):
        return "failure"
    return "incomplete"
