"""Bounded session-scoped remediation-debt sidecar."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from grokbuild.persist import (
    MAX_LOG_BYTES,
    _rewrite_jsonl,
    sidecar_generations,
    append_jsonl,
    state_lock,
    sidecar_path,
)
from grokbuild.policy import load_profiles

REMEDIATION_SCHEMA = "remediation-debt-v1"
_VALID_REVERSIBILITY = frozenset({"reversible", "unknown", "irreversible"})
# Obligations are a read-modify-append log: every append is a full snapshot, so
# keeping the newest record per (session, obligation) is lossless. Compact well
# before append_jsonl's size rotation would move records out of the read set.
REMEDIATION_COMPACT_LINES = 400
REMEDIATION_TTL = 24 * 60 * 60.0


@dataclass(frozen=True)
class RemediationDebt:
    schema: str
    session_id: str
    obligation: str
    decision_references: tuple[str, ...]
    current_branch: str
    safe_alternatives_considered: tuple[dict[str, str], ...]
    attempt_outcomes: tuple[dict[str, Any], ...]
    unresolved_operations: tuple[dict[str, str], ...]
    reversibility: str
    evidence_references: tuple[str, ...]
    last_update: float
    resolved: bool = False
    stop_blocks: int = 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["decision_references"] = list(self.decision_references)
        payload["safe_alternatives_considered"] = list(self.safe_alternatives_considered)
        payload["attempt_outcomes"] = list(self.attempt_outcomes)
        payload["unresolved_operations"] = list(self.unresolved_operations)
        payload["evidence_references"] = list(self.evidence_references)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RemediationDebt | None:
        if data.get("schema") != REMEDIATION_SCHEMA:
            return None
        reversibility = str(data.get("reversibility") or "unknown")
        if reversibility not in _VALID_REVERSIBILITY:
            reversibility = "unknown"
        session_id = str(data.get("session_id") or "")
        obligation = str(data.get("obligation") or "")
        if not session_id or not obligation:
            return None
        return cls(
            schema=REMEDIATION_SCHEMA,
            session_id=session_id,
            obligation=obligation,
            decision_references=tuple(
                str(item) for item in (data.get("decision_references") or ()) if item
            ),
            current_branch=str(data.get("current_branch") or ""),
            safe_alternatives_considered=tuple(
                dict(item)
                for item in (data.get("safe_alternatives_considered") or ())
                if isinstance(item, Mapping)
            ),
            attempt_outcomes=tuple(
                dict(item)
                for item in (data.get("attempt_outcomes") or ())
                if isinstance(item, Mapping)
            ),
            unresolved_operations=tuple(
                dict(item)
                for item in (data.get("unresolved_operations") or ())
                if isinstance(item, Mapping)
            ),
            reversibility=reversibility,
            evidence_references=tuple(
                str(item) for item in (data.get("evidence_references") or ()) if item
            ),
            last_update=float(data.get("last_update") or 0.0),
            resolved=bool(data.get("resolved", False)),
            stop_blocks=max(0, int(data.get("stop_blocks") or 0)),
        )


def remediation_path() -> Path:
    return sidecar_path("remediation-debt-v1.jsonl")


def _generations(path: Path) -> tuple[Path, ...]:
    """Return oldest-to-newest generations, so the newest record wins the fold.

    ``append_jsonl`` rotates this sidecar once it reaches its size cap. Reading
    only the current file would drop every open obligation the moment a rotation
    happened, and the Stop gate would silently stop enforcing them.
    """
    return tuple(candidate for candidate in sidecar_generations(path)[::-1] if candidate.is_file())


def _read_records(path: Path) -> list[RemediationDebt]:
    records: list[RemediationDebt] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"unable to read remediation generation: {path}") from exc
    for index, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"quarantined invalid remediation history: {path}:{index + 1}", file=sys.stderr)
            continue
        if not isinstance(raw, Mapping):
            print(f"quarantined invalid remediation record: {path}:{index + 1}", file=sys.stderr)
            continue
        record = RemediationDebt.from_dict(raw)
        if record is None:
            print(f"quarantined invalid remediation record: {path}:{index + 1}", file=sys.stderr)
            continue
        records.append(record)
    return records


def _records(path: Path) -> list[RemediationDebt]:
    records: list[RemediationDebt] = []
    for generation in _generations(path):
        records.extend(_read_records(generation))
    return records


def _latest(path: Path) -> dict[tuple[str, str], RemediationDebt]:
    result: dict[tuple[str, str], RemediationDebt] = {}
    for record in _records(path):
        result[(record.session_id, record.obligation)] = record
    return result


def compact_remediation_debt(
    path: Path | str | None = None,
    *,
    now: float | None = None,
    ttl: float = REMEDIATION_TTL,
    debt_window_seconds: float | None = None,
) -> tuple[int, int]:
    """Rewrite the sidecar to the newest record per obligation.

    Returns ``(before, after)`` record counts. Every append is a full snapshot
    rebuilt from the previous record, so keeping only the newest one per
    ``(session_id, obligation)`` loses nothing a reader can observe. Records
    past ``ttl`` are dropped: ``open_remediation_debts`` already filters them
    out by the profile's debt window. The caller must hold the sidecar lock.
    """
    target = remediation_path() if path is None else Path(path)
    stamp = time.time() if now is None else now
    if debt_window_seconds is None:
        try:
            profile_name = os.environ.get("GROK_ROUTE_PROFILE", "default")
            profile = load_profiles().get(profile_name)
            resolved_window = float(profile.debt_window_seconds) if profile is not None else ttl
            if resolved_window >= 0:
                debt_window_seconds = resolved_window
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if debt_window_seconds is not None:
        ttl = max(ttl, debt_window_seconds)
    before = sum(len(_read_records(generation)) for generation in _generations(target))
    if not before:
        return 0, 0
    kept = [
        record
        for record in _latest(target).values()
        if stamp - record.last_update <= ttl or record.last_update > stamp
    ]
    kept.sort(key=lambda record: (record.last_update, record.session_id, record.obligation))
    _rewrite_jsonl(target, [record.to_dict() for record in kept])
    for generation in _generations(target):
        if generation != target:
            generation.unlink(missing_ok=True)
    return before, len(kept)


def _append(
    record: RemediationDebt,
    path: Path,
    *,
    debt_window_seconds: float | None = None,
) -> None:
    # Compact while all generations are still readable, before size rotation
    # can discard the oldest snapshot containing an open obligation.
    try:
        line_count = sum(len(_read_records(generation)) for generation in _generations(path))
        needs_compaction = line_count >= REMEDIATION_COMPACT_LINES
        if path.is_file() and path.stat().st_size >= MAX_LOG_BYTES:
            needs_compaction = True
    except OSError:
        needs_compaction = False
    if needs_compaction:
        compact_remediation_debt(path, debt_window_seconds=debt_window_seconds)
    append_jsonl(path, record.to_dict())


def open_remediation_debt(
    *,
    session_id: str | None,
    decision_id: str | None,
    reason_code: str,
    current_branch: str,
    safe_alternative: Mapping[str, str],
    path: Path | str | None = None,
    now: float | None = None,
    debt_window_seconds: float | None = None,
) -> RemediationDebt | None:
    """Open or refresh one denial obligation without duplicating alternatives."""
    if not session_id or not reason_code:
        return None
    target = remediation_path() if path is None else Path(path)
    stamp = time.time() if now is None else now
    obligation = reason_code
    hint = {str(key): str(value) for key, value in safe_alternative.items()}
    reversibility = hint.get("reversibility", "unknown")
    if reversibility not in _VALID_REVERSIBILITY:
        reversibility = "unknown"
    operation = {
        "operation": reason_code,
        "reversibility": reversibility,
        "scope_impact": "routing-stage obligation",
    }
    with state_lock(target, lock_name="remediation-debt-v1.lock"):
        previous = _latest(target).get((session_id, obligation))
        alternatives = list(previous.safe_alternatives_considered if previous else ())
        if hint not in alternatives:
            alternatives.append(hint)
        unresolved = list(previous.unresolved_operations if previous else ())
        if operation not in unresolved:
            unresolved.append(operation)
        references = list(previous.decision_references if previous else ())
        if decision_id and decision_id not in references:
            references.append(decision_id)
        record = RemediationDebt(
            schema=REMEDIATION_SCHEMA,
            session_id=session_id,
            obligation=obligation,
            decision_references=tuple(references),
            current_branch=current_branch or hint.get("kind", reason_code),
            safe_alternatives_considered=tuple(alternatives),
            attempt_outcomes=previous.attempt_outcomes if previous else (),
            unresolved_operations=tuple(unresolved),
            reversibility=reversibility,
            evidence_references=previous.evidence_references if previous else (),
            last_update=stamp,
            resolved=False,
            stop_blocks=previous.stop_blocks if previous else 0,
        )
        _append(record, target, debt_window_seconds=debt_window_seconds)
        return record


def record_remediation_attempt(
    *,
    session_id: str | None,
    decision_id: str,
    branch: str,
    success: bool,
    evidence_references: tuple[str, ...] = (),
    path: Path | str | None = None,
    now: float | None = None,
) -> int:
    """Update matching obligations for a child result, deduplicating retries."""
    if not session_id or not decision_id:
        return 0
    target = remediation_path() if path is None else Path(path)
    stamp = time.time() if now is None else now
    updated = 0
    with state_lock(target, lock_name="remediation-debt-v1.lock"):
        matching = [
            record
            for record in _latest(target).values()
            if record.session_id == session_id
            and decision_id in record.decision_references
            and not record.resolved
        ]
        for record in matching:
            attempt = {
                "decision_id": decision_id,
                "branch": branch,
                "outcome": "success" if success else "failure",
                "evidence_references": list(evidence_references),
            }
            if attempt in record.attempt_outcomes:
                continue
            refs = tuple(dict.fromkeys((*record.evidence_references, *evidence_references)))
            next_record = replace(
                record,
                current_branch=branch,
                attempt_outcomes=(*record.attempt_outcomes, attempt),
                evidence_references=refs,
                unresolved_operations=() if success else record.unresolved_operations,
                reversibility="reversible" if success else record.reversibility,
                last_update=stamp,
                resolved=success,
                stop_blocks=0 if success else record.stop_blocks,
            )
            _append(next_record, target)
            updated += 1
    return updated


def open_remediation_debts(
    session_id: str | None,
    window_seconds: float,
    *,
    path: Path | str | None = None,
    now: float | None = None,
) -> tuple[RemediationDebt, ...]:
    if not session_id or window_seconds < 0:
        return ()
    target = remediation_path() if path is None else Path(path)
    stamp = time.time() if now is None else now
    # Stop and consumers must observe a coherent generation set; unreadable
    # history raises instead of being interpreted as an empty debt set.
    with state_lock(target, lock_name="remediation-debt-v1.lock"):
        current = _latest(target)
    return tuple(
        sorted(
            (
                record
                for record in current.values()
                if record.session_id == session_id
                and not record.resolved
                and stamp - record.last_update <= window_seconds
            ),
            key=lambda record: (record.last_update, record.obligation),
            reverse=True,
        )
    )


def record_remediation_stop(
    record: RemediationDebt,
    *,
    path: Path | str | None = None,
    now: float | None = None,
) -> RemediationDebt:
    target = remediation_path() if path is None else Path(path)
    with state_lock(target, lock_name="remediation-debt-v1.lock"):
        current = _latest(target).get((record.session_id, record.obligation), record)
        updated = replace(
            current,
            stop_blocks=current.stop_blocks + 1,
            last_update=time.time() if now is None else now,
        )
        _append(updated, target)
    return updated


def remediation_stop_context(record: RemediationDebt) -> dict[str, object]:
    reversible_count = sum(
        alternative.get("reversibility") == "reversible"
        for alternative in record.safe_alternatives_considered
    )
    failed_attempts = sum(
        attempt.get("outcome") == "failure" for attempt in record.attempt_outcomes
    )
    reversible = reversible_count > failed_attempts
    irreversible_only = bool(record.unresolved_operations) and all(
        operation.get("reversibility") == "irreversible"
        for operation in record.unresolved_operations
    )
    if irreversible_only:
        action = "BLOCKED+escalate"
        requested = "Decide whether to authorize the irreversible operation."
    elif reversible:
        action = "continue_safe_remediation"
        requested = "Continue the remaining reversible alternative."
    else:
        action = "hold_for_evidence"
        requested = "Classify reversibility before authorizing any action."
    return {
        "action": action,
        "blocked_action": record.obligation,
        "attempted_alternatives": list(record.attempt_outcomes),
        "evidence_references": list(record.evidence_references),
        "scope_impact": [
            operation.get("scope_impact", "unknown") for operation in record.unresolved_operations
        ],
        "exact_requested_decision": requested,
    }


__all__ = [
    "REMEDIATION_SCHEMA",
    "RemediationDebt",
    "compact_remediation_debt",
    "open_remediation_debt",
    "open_remediation_debts",
    "record_remediation_attempt",
    "record_remediation_stop",
    "remediation_path",
    "remediation_stop_context",
]
