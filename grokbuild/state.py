#!/usr/bin/env python3
"""Local runtime state for the Grok Build combine router.

Tracks per-role availability, a failure/success circuit breaker, quota counters,
a bounded decision history, and per-session turn counters. All writes are
transactional: a `flock` on a `.lock` file + a unique `tempfile.mkstemp` +
`fsync` + `os.replace`, and files are chmod 0600. A state file older than
`STALE_AFTER` is flagged stale so callers do not trust availability/circuit data
that may describe a dead process.

The hook *cannot* verify real model availability; the circuit breaker is fed by
explicit signals (CLI `state set`, observed denials, completion records) and is
advisory, never a hard block on its own.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from grokbuild.redact import redact
from grokbuild.decision import ExecutionMember, ExecutionStage
from grokbuild.persist import (
    HISTORY_LIMIT,
    HISTORY_ROTATE_LINES,  # noqa: F401
    MAX_LOG_BYTES,  # noqa: F401
    MAX_LOG_FILES,  # noqa: F401
    _append_decisions_locked,
    _decisions_path,
    _migration_marker,
    _read_decision_history,
    _read_decision_history_unbounded,
    atomic_update_json,
    _write_marker,
    sidecar_path,
)

STATE_VERSION = 8
TEMP_GC_SCAN_LIMIT = 100
TEMP_GC_DELETE_LIMIT = 50
STALE_AFTER = 24 * 60 * 60.0  # 24h
EXECUTION_TTL = 24 * 60 * 60.0  # prune old per-decision executor tracks
TURN_TTL = 24 * 60 * 60.0  # prune per-session turn/prompt bookkeeping
PROVIDER_AVAILABILITY_TTL = 24 * 60 * 60.0
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_CONSILIUM_THRESHOLD = 3


def default_state_path() -> Path:
    return sidecar_path("state.json")


def default_log_path() -> Path:
    return sidecar_path("route.jsonl")


@dataclass
class ProviderAvailability:
    available: bool = True
    unavailable_until: float = 0.0
    reason: str = ""
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "unavailable_until": self.unavailable_until,
            "reason": self.reason,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProviderAvailability":
        def number(name: str) -> float:
            try:
                return float(data.get(name, 0.0))
            except (TypeError, ValueError):
                return 0.0

        return cls(
            available=bool(data.get("available", True)),
            unavailable_until=number("unavailable_until"),
            reason=str(data.get("reason", "")),
            last_seen=number("last_seen"),
        )


@dataclass
class RoleStatus:
    available: bool = True
    reason: str = ""
    availability_signal: str = ""
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    requested: int = 0
    completed: int = 0
    quota_used: int = 0
    quota_limit: int | None = None
    circuit_open_until: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "availability_signal": self.availability_signal,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "requested": self.requested,
            "completed": self.completed,
            "quota_used": self.quota_used,
            "quota_limit": self.quota_limit,
            "circuit_open_until": self.circuit_open_until,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoleStatus":
        return cls(
            available=bool(data.get("available", True)),
            reason=str(data.get("reason", "")),
            availability_signal=str(data.get("availability_signal", "")),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            consecutive_successes=int(data.get("consecutive_successes", 0)),
            total_failures=int(data.get("total_failures", 0)),
            total_successes=int(data.get("total_successes", 0)),
            requested=int(data.get("requested", 0)),
            completed=int(data.get("completed", 0)),
            quota_used=int(data.get("quota_used", 0)),
            quota_limit=data.get("quota_limit"),
            circuit_open_until=float(data.get("circuit_open_until", 0.0)),
            last_seen=float(data.get("last_seen", 0.0)),
        )

    def quota_pressure(self) -> float:
        if self.quota_limit and self.quota_used >= self.quota_limit:
            return 1.0
        if self.quota_limit and self.quota_limit > 0:
            return max(0.0, min(1.0, self.quota_used / self.quota_limit))
        return 0.0


def _argv_has_shell_metachar(argv: tuple[str, ...]) -> bool:
    """Return whether tokens carry rejected shell metacharacters or controls."""
    return any(
        any(ch in token for ch in ("\n", "\r", "\x00", "|", "&", ";", "<", ">", "`", "$("))
        for token in argv
    )


def _canonical_stage_slot(stage: ExecutionStage, kind_index: int) -> str:
    """Return the canonical execution slot using the pipeline branch order."""
    if stage.kind == "verify":
        return stage.stage_id if stage.stage_id.startswith("verify/") else f"verify/{kind_index}"
    if stage.stage_id in {"recon", "consilium", "consilium-unavailable", "planner-market"}:
        return stage.stage_id
    if stage.stage_id.startswith("review") or stage.role.startswith("review"):
        return "review"
    if stage.role == "recon" or stage.role.startswith("explore"):
        return "recon"
    if stage.role.startswith("implement"):
        return "impl"
    # The planner market must not collapse into the ordinary plan slot: two
    # stages sharing a (slot, kind) pair make reconciliation ambiguous and drop
    # their recorded progress.
    if stage.role == "plan-comparator":
        return "plan-comparator"
    if stage.role.startswith("planner-"):
        return "planner-market"
    if stage.role.startswith("plan"):
        return "plan"
    if stage.role == "security":
        return "security"
    if stage.role == "security-verify":
        return "security-verify"
    return f"{stage.kind}/{kind_index}"


def _stage_slot(stage: ExecutionStage, kind_index: int) -> str:
    if stage.slot:
        return stage.slot
    return _canonical_stage_slot(stage, kind_index)


def _with_stage_slots(stages: tuple[ExecutionStage, ...]) -> tuple[ExecutionStage, ...]:
    counts: dict[str, int] = {}
    slotted: list[ExecutionStage] = []
    for stage in stages:
        kind_index = counts.get(stage.kind, 0)
        counts[stage.kind] = kind_index + 1
        slot = _stage_slot(stage, kind_index)
        slotted.append(stage if stage.slot == slot else replace(stage, slot=slot))
    return tuple(slotted)


@dataclass
class ExecutionTrack:
    """Per-decision multi-stage executor progress (persisted across hook calls).

    Spawnable stages are tracked via ``requested``/``completed``/``failed``.
    Deterministic verification stages (``kind == "verify"``) are tracked
    separately in ``verified`` — they are never spawned and never feed the
    spawn circuit breaker.
    """

    decision_id: str
    session_id: str | None
    turn_id: int | None
    stages: tuple[ExecutionStage, ...] = ()
    requested: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    member_tasks: dict[str, str] = field(default_factory=dict)
    stage_tasks: dict[str, str] = field(default_factory=dict)
    terminal_tasks: dict[str, str] = field(default_factory=dict)
    verify_tasks: dict[str, str] = field(default_factory=dict)
    released_verify_tasks: dict[str, str] = field(default_factory=dict)
    requested_at: dict[str, float] = field(default_factory=dict)
    repair_failures: dict[str, int] = field(default_factory=dict)
    not_found_streaks: dict[str, dict] = field(default_factory=dict)
    failure_reasons: dict[str, str] = field(default_factory=dict)
    # At most one reactive endpoint retry is allowed per stage/member.
    reactive_respawns: dict[str, dict[str, str | int | float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    migrated_dropped: list[str] = field(default_factory=list)
    stop_blocks: int = 0
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def member_key(stage_id: str, member_id: str) -> str:
        return f"{stage_id}/{member_id}"

    def _member_entries(self):
        for stage in self.stages:
            if stage.kind == "parallel_spawn":
                for member in stage.members:
                    yield stage, member, self.member_key(stage.stage_id, member.member_id)

    def member_for_key(self, key: str) -> ExecutionMember | None:
        for _stage, member, candidate in self._member_entries():
            if candidate == key:
                return member
        return None

    def review_inconclusive(self, stage_key: str) -> bool:
        return f"review_inconclusive:{stage_key}" in self.warnings

    def incomplete_required_members(self, stage: ExecutionStage) -> list[ExecutionMember]:
        if stage.kind != "parallel_spawn":
            return []
        return [
            member
            for member in stage.members
            if member.required
            and self.member_key(stage.stage_id, member.member_id) not in self.completed
            and not self.review_inconclusive(self.member_key(stage.stage_id, member.member_id))
        ]

    def next_incomplete_required_stage(self) -> ExecutionStage | None:
        """Return the first unfinished required stage in composition order."""
        for stage in self.stages:
            if not stage.required:
                continue
            if stage.kind == "parallel_spawn":
                if self.incomplete_required_members(stage):
                    return stage
            elif stage.kind == "verify":
                if (stage.stage_id or stage.role) not in self.verified:
                    return stage
            elif stage.role not in self.completed and not self.review_inconclusive(stage.role):
                return stage
        return None

    def next_required_barrier_or_role(self) -> ExecutionStage | str | None:
        """Return fail-closed runtime barriers before ordinary required stages."""
        sentinel = next(
            (stage for stage in self.stages if stage.required and stage.kind == "sentinel"),
            None,
        )
        if sentinel is not None:
            return sentinel.role
        consilium = next(
            (
                stage
                for stage in self.stages
                if stage.required
                and stage.spawnable
                and stage.kind == "parallel_spawn"
                and stage.stage_id == "consilium"
                and self.incomplete_required_members(stage)
            ),
            None,
        )
        if consilium is not None:
            return consilium
        stage = self.next_incomplete_required_stage()
        if stage is None or not stage.spawnable:
            return None
        if stage.kind == "parallel_spawn":
            return stage
        return stage.role

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "stages": [stage.to_dict() for stage in self.stages],
            "requested": list(self.requested),
            "completed": list(self.completed),
            "failed": list(self.failed),
            "verified": list(self.verified),
            "member_tasks": dict(self.member_tasks),
            "stage_tasks": dict(self.stage_tasks),
            "terminal_tasks": dict(self.terminal_tasks),
            "verify_tasks": dict(self.verify_tasks),
            **({"released_verify_tasks": dict(self.released_verify_tasks)} if self.released_verify_tasks else {}),
            "requested_at": dict(self.requested_at),
            "repair_failures": dict(self.repair_failures),
            "not_found_streaks": dict(self.not_found_streaks),
            "failure_reasons": dict(self.failure_reasons),
            **({"reactive_respawns": self.reactive_respawns} if self.reactive_respawns else {}),
            "warnings": list(self.warnings),
            "migrated_dropped": list(self.migrated_dropped),
            "stop_blocks": self.stop_blocks,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionTrack":
        stages = _with_stage_slots(
            tuple(
                ExecutionStage.from_dict(item)
                if isinstance(item, Mapping)
                else ExecutionStage("", False, "")
                for item in (data.get("stages") or [])
            )
        )

        def migrate(values: Any) -> list[str]:
            migrated: list[str] = []
            claimed: set[str] = set()
            for value in values or []:
                token = str(value)
                if "/" in token:
                    migrated.append(token)
                    continue
                replacement = token
                for stage in stages:
                    if stage.kind != "parallel_spawn":
                        continue
                    for member in stage.members:
                        key = cls.member_key(stage.stage_id, member.member_id)
                        if member.role == token and key not in claimed:
                            replacement = key
                            claimed.add(key)
                            break
                    if replacement != token:
                        break
                migrated.append(replacement)
            return migrated

        raw_requested_at = data.get("requested_at")
        requested_at: dict[str, float] = {}
        if isinstance(raw_requested_at, Mapping):
            for key, value in raw_requested_at.items():
                try:
                    requested_at[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        repair_failures: dict[str, int] = {}
        raw_repair_failures = data.get("repair_failures")
        if isinstance(raw_repair_failures, Mapping):
            for key, value in raw_repair_failures.items():
                try:
                    repair_failures[str(key)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        not_found_streaks: dict[str, dict] = {}
        raw_not_found_streaks = data.get("not_found_streaks")
        if isinstance(raw_not_found_streaks, Mapping):
            for key, value in raw_not_found_streaks.items():
                if not isinstance(value, Mapping) or not value.get("task_id"):
                    continue
                try:
                    not_found_streaks[str(key)] = {
                        "task_id": str(value["task_id"]),
                        "count": max(0, int(value.get("count", 0))),
                    }
                except (TypeError, ValueError):
                    continue
        failure_reasons: dict[str, str] = {}
        raw_failure_reasons = data.get("failure_reasons")
        if isinstance(raw_failure_reasons, Mapping):
            failure_reasons = {
                str(key): value
                for key, value in raw_failure_reasons.items()
                if key and isinstance(value, str) and value
            }
        reactive_respawns: dict[str, dict[str, str | int | float]] = {}
        raw_reactive = data.get("reactive_respawns")
        if isinstance(raw_reactive, Mapping):
            for key, value in raw_reactive.items():
                if not isinstance(value, Mapping):
                    continue
                try:
                    count = max(0, int(value.get("count", 0)))
                except (TypeError, ValueError):
                    continue
                if count:
                    marker = {
                        "count": count,
                        "from_provider": str(value.get("from_provider", "")),
                        "to_provider": str(value.get("to_provider", "")),
                        "role": str(value.get("role", "")),
                    }
                    try:
                        unavailable_until = float(value.get("unavailable_until", 0.0))
                    except (TypeError, ValueError):
                        unavailable_until = 0.0
                    if unavailable_until:
                        marker["unavailable_until"] = unavailable_until
                    reactive_respawns[str(key)] = marker

        return cls(
            decision_id=str(data.get("decision_id", "")),
            session_id=data.get("session_id"),
            turn_id=data.get("turn_id"),
            stages=stages,
            requested=migrate(data.get("requested")),
            completed=migrate(data.get("completed")),
            failed=migrate(data.get("failed")),
            verified=[str(x) for x in (data.get("verified") or [])],
            member_tasks={str(k): str(v) for k, v in (data.get("member_tasks") or {}).items()},
            stage_tasks={str(k): str(v) for k, v in (data.get("stage_tasks") or {}).items()},
            terminal_tasks={str(k): str(v) for k, v in (data.get("terminal_tasks") or {}).items()},
            verify_tasks={str(k): str(v) for k, v in (data.get("verify_tasks") or {}).items()},
            released_verify_tasks={str(k): str(v) for k, v in (data.get("released_verify_tasks") or {}).items()},
            requested_at=requested_at,
            repair_failures=repair_failures,
            not_found_streaks=not_found_streaks,
            failure_reasons=failure_reasons,
            reactive_respawns=reactive_respawns,
            warnings=[str(x) for x in (data.get("warnings") or [])],
            migrated_dropped=[str(x) for x in (data.get("migrated_dropped") or [])],
            stop_blocks=int(data.get("stop_blocks", 0)),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def required_roles(self) -> list[str]:
        """Required linear spawn roles (parallel members use member keys)."""
        return [
            stage.role
            for stage in self.stages
            if stage.required and stage.spawnable and stage.kind != "parallel_spawn"
        ]

    def required_verify_steps(self) -> list[str]:
        """Required deterministic verification step ids."""
        return [
            stage.stage_id or stage.role
            for stage in self.stages
            if stage.required and not stage.spawnable
        ]

    def next_required_role(self) -> str | None:
        current = self.next_required_barrier_or_role()
        return current if isinstance(current, str) else None

    def next_verify_step(self) -> str | None:
        # Verification cannot run ahead of any unfinished spawn/barrier.
        if self.next_required_barrier_or_role() is not None:
            return None
        for step in self.required_verify_steps():
            if step not in self.verified:
                return step
        return None

    def all_required_completed(self) -> bool:
        if self.next_required_barrier_or_role() is not None:
            return False
        return all(step in self.verified for step in self.required_verify_steps())


class LazyDecisionHistory(list[dict[str, Any]]):
    """Load bounded decision history only when a reader accesses it."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.loaded = False

    def _load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        super().extend(_read_decision_history(self.path))

    def __len__(self) -> int:
        self._load()
        return super().__len__()

    def __iter__(self):
        self._load()
        return super().__iter__()

    def __getitem__(self, item):
        self._load()
        return super().__getitem__(item)

    def __contains__(self, item: object) -> bool:
        self._load()
        return super().__contains__(item)

    def __eq__(self, other: object) -> bool:
        self._load()
        return super().__eq__(other)

    def __repr__(self) -> str:
        self._load()
        return super().__repr__()

    def append(self, item: dict[str, Any]) -> None:
        self._load()
        super().append(item)


@dataclass
class RuntimeState:
    version: int = STATE_VERSION
    roles: dict[str, RoleStatus] = field(default_factory=dict)
    provider_availability: dict[str, ProviderAvailability] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: dict[str, int] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    turn_seen: dict[str, float] = field(default_factory=dict)
    executions: dict[str, ExecutionTrack] = field(default_factory=dict)
    # Hook-observed spawn-result circuits are scoped to one conductor session.
    # Global role availability remains reserved for explicit operator overrides.
    session_circuits: dict[str, dict[str, RoleStatus]] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    stale: bool = False
    source_path: Path | None = None

    def status_for(self, role: str | None) -> RoleStatus:
        if not role:
            return RoleStatus()
        return self.roles.setdefault(role, RoleStatus())

    def is_provider_available(self, provider: str | None, now: float | None = None) -> bool:
        if not provider:
            return True
        status = self.provider_availability.get(provider)
        if status is None:
            return True
        now = time.time() if now is None else now
        if status.unavailable_until > now:
            return False
        return True

    def set_provider_unavailable(
        self, provider: str, until: float, reason: str, now: float | None = None
    ) -> None:
        now = time.time() if now is None else now
        self.provider_availability[provider] = ProviderAvailability(
            available=False, unavailable_until=float(until), reason=reason, last_seen=now
        )
        self.updated_at = now

    def clear_provider_unavailable(self, provider: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        status = self.provider_availability.setdefault(provider, ProviderAvailability())
        status.available = True
        status.unavailable_until = 0.0
        status.reason = ""
        status.last_seen = now
        self.updated_at = now

    def provider_availability_snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            name: {
                **status.to_dict(),
                "available": self.is_provider_available(name, now=now),
                "expired": status.unavailable_until <= now,
            }
            for name, status in sorted(self.provider_availability.items())
        }

    def session_status_for(self, session_id: str | None, role: str | None) -> RoleStatus:
        if not session_id or not role:
            return RoleStatus()
        return self.session_circuits.setdefault(session_id, {}).setdefault(role, RoleStatus())

    def is_available(
        self, role: str | None, now: float | None = None, session_id: str | None = None
    ) -> bool:
        if not role:
            return True
        status = self.status_for(role)
        now = time.time() if now is None else now
        # `available=False` is an explicit global/operator override.
        if not status.available:
            return False
        scoped = self.session_status_for(session_id, role) if session_id else None
        return not bool(scoped and scoped.circuit_open_until and now < scoped.circuit_open_until)

    def circuit_open(
        self, role: str | None, now: float | None = None, session_id: str | None = None
    ) -> bool:
        if not role:
            return False
        now = time.time() if now is None else now
        if session_id:
            status = self.session_status_for(session_id, role)
            return bool(status.circuit_open_until and now < status.circuit_open_until)
        status = self.status_for(role)
        return bool(status.circuit_open_until and now < status.circuit_open_until)

    def current_turn(self, session_id: str | None) -> int | None:
        if not session_id:
            return None
        return self.turns.get(session_id, 0)

    def get_execution(self, decision_id: str) -> ExecutionTrack | None:
        return self.executions.get(decision_id)

    def latest_session_debt(
        self,
        session_id: str | None,
        window_seconds: float,
        now: float | None = None,
        excluded_decision_id: str | None = None,
        max_stop_blocks: int | None = None,
    ) -> tuple[str, ExecutionTrack] | None:
        """Return the newest recent incomplete execution for one session."""
        if not session_id or window_seconds < 0:
            return None
        now = time.time() if now is None else now
        candidates = (
            (decision_id, track)
            for decision_id, track in self.executions.items()
            if decision_id != excluded_decision_id
            and track.session_id == session_id
            and (max_stop_blocks is None or track.stop_blocks < max_stop_blocks)
            and not track.all_required_completed()
            and now - track.updated_at <= window_seconds
        )
        return max(candidates, key=lambda item: (item[1].updated_at, item[0]), default=None)

    def set_execution(
        self,
        decision_id: str,
        session_id: str | None,
        turn_id: int | None,
        stages: tuple[ExecutionStage, ...],
    ) -> ExecutionTrack:
        stages = _with_stage_slots(stages)
        track = self.executions.get(decision_id)
        if track is None:
            track = ExecutionTrack(
                decision_id=decision_id, session_id=session_id, turn_id=turn_id, stages=stages
            )
            self.executions[decision_id] = track
        else:
            if stages and stages != track.stages:
                track.stages = stages
            track.session_id = session_id
            track.turn_id = turn_id
            track.updated_at = time.time()
        return track

    def record_request(self, role: str | None, now: float | None = None) -> None:
        if not role:
            return
        now = time.time() if now is None else now
        status = self.status_for(role)
        status.requested += 1
        status.last_seen = now
        self.updated_at = now

    def record_completion(self, role: str | None, success: bool, now: float | None = None) -> None:
        if not role:
            return
        now = time.time() if now is None else now
        status = self.status_for(role)
        status.completed += 1
        if success:
            status.consecutive_successes += 1
            status.total_successes += 1
            status.consecutive_failures = 0
            status.available = True
            status.circuit_open_until = 0.0
            status.reason = ""
        else:
            status.consecutive_failures += 1
            status.total_failures += 1
            status.consecutive_successes = 0
        status.last_seen = now
        self.updated_at = now

    def success_rate(self, role: str | None, min_samples: int = 3) -> float | None:
        """Bounded, sample-adjusted success rate; None when under-sampled."""

        if not role:
            return None
        status = self.status_for(role)
        if status.requested < min_samples:
            return None
        return (status.total_successes + 1.0) / (status.requested + 2.0)

    def record_decision(self, decision: Any, now: float | None = None) -> None:
        now = time.time() if now is None else now
        data = redact(decision)
        self.history.append(data)
        del self.history[: max(0, len(self.history) - HISTORY_LIMIT)]
        role = data.get("role")
        if role:
            self.status_for(role).quota_used += 1
        session_id = data.get("session_id")
        turn_id = data.get("turn_id")
        if session_id and isinstance(turn_id, int):
            self.turns[session_id] = max(self.turns.get(session_id, 0), turn_id)
            self.turn_seen[session_id] = now
        self.updated_at = now
        self.stale = False

    def prune(self, now: float | None = None) -> None:
        """Drop executor tracks and turn bookkeeping older than their TTLs."""
        now = time.time() if now is None else now
        incomplete_ttl = EXECUTION_TTL
        try:
            from grokbuild.policy import load_profiles

            incomplete_ttl = max(
                EXECUTION_TTL,
                *(profile.debt_window_seconds for profile in load_profiles().values()),
            )
        except (ImportError, OSError, ValueError):
            pass
        for decision_id in [
            key
            for key, track in self.executions.items()
            if now - track.updated_at
            > (EXECUTION_TTL if track.all_required_completed() else incomplete_ttl)
        ]:
            self.executions.pop(decision_id, None)
        expired = [key for key, seen in self.turn_seen.items() if now - seen > TURN_TTL]
        # Every writer stamps `turn_seen` together with `turns`, so a session that
        # only has turn bookkeeping is unreachable residue from an older state
        # file. Ageing keyed on `turn_seen` alone can never reach it, which makes
        # it an unbounded leak rather than a bounded one.
        orphans = [key for key in self.turns if key not in self.turn_seen]
        orphans.extend(key for key in self.prompts if key not in self.turn_seen)
        for session_id in [*expired, *orphans]:
            self.turns.pop(session_id, None)
            self.turn_seen.pop(session_id, None)
            self.prompts.pop(session_id, None)
            self.session_circuits.pop(session_id, None)
        # Session circuits are opened by role failures alone, without any turn
        # bookkeeping, so they need their own clock: the newest role signal in
        # the circuit. Empty circuits are dropped outright.
        for session_id in [
            key
            for key, circuit in self.session_circuits.items()
            if not circuit or now - max(status.last_seen for status in circuit.values()) > TURN_TTL
        ]:
            self.session_circuits.pop(session_id, None)
        for provider, status in list(self.provider_availability.items()):
            if (
                status.unavailable_until <= now
                and now - status.last_seen > PROVIDER_AVAILABILITY_TTL
            ):
                self.provider_availability.pop(provider, None)

    def mark_failure(
        self,
        role: str | None,
        reason: str = "",
        *,
        threshold: int = 3,
        cooldown: float = 300.0,
        now: float | None = None,
        session_id: str | None = None,
    ) -> None:
        if not role:
            return
        now = time.time() if now is None else now
        status = self.session_status_for(session_id, role) if session_id else self.status_for(role)
        status.consecutive_failures += 1
        status.total_failures += 1
        status.consecutive_successes = 0
        status.last_seen = now
        status.reason = reason
        if status.consecutive_failures >= threshold:
            status.available = False
            status.circuit_open_until = now + cooldown
        self.updated_at = now

    def mark_success(
        self, role: str | None, now: float | None = None, session_id: str | None = None
    ) -> None:
        if not role:
            return
        now = time.time() if now is None else now
        status = self.session_status_for(session_id, role) if session_id else self.status_for(role)
        status.consecutive_successes += 1
        status.total_successes += 1
        status.consecutive_failures = 0
        status.available = True
        status.circuit_open_until = 0.0
        status.reason = ""
        status.last_seen = now
        self.updated_at = now

    def set_available(
        self,
        role: str | None,
        available: bool,
        reason: str = "",
        now: float | None = None,
    ) -> None:
        if not role:
            return
        now = time.time() if now is None else now
        status = self.status_for(role)
        status.available = available
        status.reason = reason
        # Operator-set availability is separate from transient reason/circuit
        # values, so a later success/failure can never erase the positive
        # provenance (or a stale/corrupt state revive it). A negative signal
        # clears any previous positive provenance.
        status.availability_signal = reason if (available and reason) else ""
        if available:
            status.circuit_open_until = 0.0
            status.consecutive_failures = 0
        status.last_seen = now
        self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "roles": {name: status.to_dict() for name, status in sorted(self.roles.items())},
            "provider_availability": {
                name: status.to_dict()
                for name, status in sorted(self.provider_availability.items())
            },
            "turns": dict(sorted(self.turns.items())),
            "prompts": dict(sorted(self.prompts.items())),
            "turn_seen": dict(sorted(self.turn_seen.items())),
            "executions": {key: track.to_dict() for key, track in sorted(self.executions.items())},
            "session_circuits": {
                session: {role: status.to_dict() for role, status in sorted(roles.items())}
                for session, roles in sorted(self.session_circuits.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source_path: Path | None = None) -> "RuntimeState":
        roles = {
            str(name): RoleStatus.from_dict(spec)
            for name, spec in (data.get("roles") or {}).items()
            if isinstance(spec, Mapping)
        }
        raw_providers = data.get("provider_availability", {})
        provider_availability = (
            {
                str(name): ProviderAvailability.from_dict(spec)
                for name, spec in raw_providers.items()
                if isinstance(raw_providers, Mapping) and isinstance(spec, Mapping)
            }
            if isinstance(raw_providers, Mapping)
            else {}
        )
        history = [dict(item) for item in (data.get("history") or []) if isinstance(item, Mapping)]
        turns = {}
        for key, value in (data.get("turns") or {}).items():
            try:
                turns[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        prompts = {str(k): str(v) for k, v in (data.get("prompts") or {}).items()}
        turn_seen = {}
        for key, value in (data.get("turn_seen") or {}).items():
            try:
                turn_seen[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        executions = {
            str(k): ExecutionTrack.from_dict(spec)
            for k, spec in (data.get("executions") or {}).items()
            if isinstance(spec, Mapping)
        }
        session_circuits = {
            str(session): {
                str(role): RoleStatus.from_dict(status)
                for role, status in roles.items()
                if isinstance(status, Mapping)
            }
            for session, roles in (data.get("session_circuits") or {}).items()
            if isinstance(roles, Mapping)
        }
        return cls(
            version=int(data.get("version", STATE_VERSION)),
            roles=roles,
            provider_availability=provider_availability,
            history=history[:HISTORY_LIMIT],
            turns=turns,
            prompts=prompts,
            turn_seen=turn_seen,
            executions=executions,
            session_circuits=session_circuits,
            updated_at=float(data.get("updated_at", time.time())),
            source_path=source_path,
        )


def pending_child_bindings(
    state: RuntimeState,
    session_id: str | None,
    *,
    track_limit: int = 20,
    task_limit: int = 32,
) -> list[str]:
    """Return pending spawn-bound child ids from recent session tracks."""
    if not session_id or track_limit <= 0 or task_limit <= 0:
        return []
    pending_tracks = (
        track
        for track in state.executions.values()
        if track.session_id == session_id
        and (
            any(
                stage_key not in set(track.completed) | set(track.failed)
                for bindings in (track.member_tasks, track.stage_tasks)
                for stage_key in bindings
            )
            or bool(track.terminal_tasks)
            or bool(track.released_verify_tasks)
        )
    )
    tracks = sorted(
        pending_tracks,
        key=lambda track: (track.updated_at, track.decision_id),
        reverse=True,
    )[:track_limit]
    task_ids: list[str] = []
    seen: set[str] = set()
    for track in tracks:
        terminal = set(track.completed) | set(track.failed)
        for bindings in (track.member_tasks, track.stage_tasks):
            for stage_key, task_id in bindings.items():
                if stage_key in terminal or task_id in seen:
                    continue
                seen.add(task_id)
                task_ids.append(task_id)
                if len(task_ids) >= task_limit:
                    return task_ids
        for task_id in (*track.terminal_tasks, *track.released_verify_tasks):
            if task_id in seen:
                continue
            seen.add(task_id)
            task_ids.append(task_id)
            if len(task_ids) >= task_limit:
                return task_ids
    return task_ids


def _migrate_legacy_history(data: Any, path: Path) -> Any:
    if not isinstance(data, Mapping) or not isinstance(data.get("history"), list):
        return data
    legacy = [dict(item) for item in data["history"] if isinstance(item, Mapping)]
    if legacy and not _migration_marker(path).is_file():
        existing = _read_decision_history_unbounded(_decisions_path(path))
        existing_ids = {
            str(item["decision_id"]) for item in existing if item.get("decision_id") is not None
        }
        additions = []
        for item in legacy:
            decision_id = item.get("decision_id")
            if decision_id is not None and str(decision_id) in existing_ids:
                continue
            additions.append(item)
            if decision_id is not None:
                existing_ids.add(str(decision_id))
        _append_decisions_locked(_decisions_path(path), additions)
        _write_marker(_migration_marker(path))
    result = dict(data)
    result.pop("history", None)
    return result


def load_state(path: Path | str | None = None) -> RuntimeState:
    target = Path(path) if path is not None else default_state_path()
    if not target.is_file():
        state = RuntimeState(source_path=target)
        state.history = LazyDecisionHistory(_decisions_path(target))
        return state
    try:
        mtime = target.stat().st_mtime
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RuntimeState(source_path=target, stale=True)
    if not isinstance(data, Mapping):
        return RuntimeState(source_path=target, stale=True)
    if "history" in data:
        data = atomic_update_json(
            target,
            lambda raw: raw,
            default=data,
            on_load=lambda raw: _migrate_legacy_history(raw, target),
        )
        mtime = target.stat().st_mtime
    state = RuntimeState.from_dict(data, source_path=target)
    state.history = LazyDecisionHistory(_decisions_path(target))
    state.stale = (time.time() - mtime) > STALE_AFTER
    state.prune()
    return state


def save_state(state: RuntimeState, path: Path | str | None = None) -> None:
    target = Path(path) if path is not None else (state.source_path or default_state_path())
    state.prune()
    atomic_update_json(
        target,
        lambda _raw: state.to_dict(),
        on_load=lambda raw: _migrate_legacy_history(raw, target),
    )


def current_turn_tx(path: Path | str | None = None, session_id: str | None = None) -> int | None:
    if not session_id:
        return None
    return load_state(path).current_turn(session_id)


__all__ = [
    "STATE_VERSION",
    "HISTORY_LIMIT",
    "STALE_AFTER",
    "EXECUTION_TTL",
    "TURN_TTL",
    "MAX_LOG_BYTES",
    "MAX_LOG_FILES",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_CONSILIUM_THRESHOLD",
    "RoleStatus",
    "ProviderAvailability",
    "ExecutionTrack",
    "RuntimeState",
    "pending_child_bindings",
    "load_state",
    "save_state",
    "current_turn_tx",
    "default_state_path",
    "default_log_path",
    "_canonical_stage_slot",
    "_with_stage_slots",
]
