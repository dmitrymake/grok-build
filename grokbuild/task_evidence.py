"""Versioned typed task contracts and their out-of-band evidence sidecar.

R2 replaces "a terminal non-empty result exists" with "the runtime verified
typed evidence against the applicable contract". This module owns the wire
format and the persistence for that evidence; :mod:`grokbuild.task_verify`
owns the acceptance rule and stays pure.

Nothing here touches state v8 or log v4. A ``TaskSpec`` is attached before the
child spawns and is immutable for that attempt: the first record for a
``(decision_id, stage_key)`` wins and later attachments are ignored, so a
reviewer cites the same contract the runtime enforced. Records ride a
versioned, redacted, size-rotated JSONL sidecar of their own, and readers fold
the rotated generations so a rotation cannot silently drop a contract.
"""

from __future__ import annotations

import fcntl
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from grokbuild import persist
from grokbuild.persist import (
    _append_jsonl_locked,
    append_jsonl,
    iter_jsonl,
    sidecar_generations,
    sidecar_path,
)
from grokbuild.task_verify import Observation, Verification, _canonical_repo_path, verify

R2_CONTRACT_VERSION = 1
TASK_SPEC_SCHEMA = "r2-task-spec-v1"
TASK_RESULT_SCHEMA = "r2-task-result-v1"
TASK_EVIDENCE_SCHEMA = "r2-evidence-v1"

# Bounds mirror the existing sidecar discipline: records stay small enough that
# redaction's per-string truncation never silently reshapes a contract.
MAX_ITEMS = 64
MAX_PATH = 400
MAX_TEXT = 500

_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*\n(.*?)\n?```", re.DOTALL)


def _text(value: object, limit: int = MAX_TEXT) -> str:
    return str(value or "")[:limit]


def _items(values: object, limit: int = MAX_PATH) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return ()
    return tuple(str(value)[:limit] for value in values[:MAX_ITEMS])


def _mappings(values: object, keys: Sequence[str]) -> tuple[dict[str, Any], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return ()
    records: list[dict[str, Any]] = []
    for value in values[:MAX_ITEMS]:
        if not isinstance(value, Mapping):
            continue
        entry: dict[str, Any] = {}
        for key in keys:
            raw = value.get(key)
            entry[key] = raw if isinstance(raw, bool) else _text(raw)
        records.append(entry)
    return tuple(records)


@dataclass(frozen=True)
class TaskSpec:
    """What "done" means for one stage attempt, fixed before the child starts."""

    decision_id: str
    stage_key: str
    role: str
    session_id: str | None = None
    path_scope: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    criteria: tuple[dict[str, Any], ...] = ()
    created_at: str = ""
    contract_version: int = R2_CONTRACT_VERSION
    schema: str = TASK_SPEC_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "stage_key": self.stage_key,
            "role": self.role,
            "path_scope": list(self.path_scope),
            "artifacts": list(self.artifacts),
            "checks": list(self.checks),
            "criteria": [dict(item) for item in self.criteria],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec | None":
        if data.get("schema") != TASK_SPEC_SCHEMA:
            return None
        try:
            contract_version = int(data.get("contract_version", 0))
        except (TypeError, ValueError):
            return None
        decision_id = _text(data.get("decision_id"))
        stage_key = _text(data.get("stage_key"))
        if not decision_id or not stage_key:
            return None
        session_id = data.get("session_id")
        return cls(
            decision_id=decision_id,
            stage_key=stage_key,
            role=_text(data.get("role")),
            session_id=_text(session_id) if session_id is not None else None,
            path_scope=_items(data.get("path_scope")),
            artifacts=_items(data.get("artifacts")),
            checks=_items(data.get("checks")),
            criteria=_mappings(data.get("criteria"), ("kind", "expected")),
            created_at=_text(data.get("created_at")),
            contract_version=contract_version,
        )


@dataclass(frozen=True)
class TaskResult:
    """The child's structured claim. A claim, never a certification."""

    decision_id: str
    stage_key: str
    status: str
    task_id: str = ""
    changed_paths: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    criteria: tuple[dict[str, Any], ...] = ()
    unresolved: tuple[str, ...] = ()
    observed_at: str = ""
    contract_version: int = R2_CONTRACT_VERSION
    schema: str = TASK_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "decision_id": self.decision_id,
            "stage_key": self.stage_key,
            "task_id": self.task_id,
            "status": self.status,
            "changed_paths": list(self.changed_paths),
            "artifacts": list(self.artifacts),
            "checks": [dict(item) for item in self.checks],
            "criteria": [dict(item) for item in self.criteria],
            "unresolved": list(self.unresolved),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskResult | None":
        if data.get("schema") != TASK_RESULT_SCHEMA:
            return None
        try:
            contract_version = int(data.get("contract_version", 0))
        except (TypeError, ValueError):
            return None
        return cls(
            decision_id=_text(data.get("decision_id")),
            stage_key=_text(data.get("stage_key")),
            status=_text(data.get("status"), 32),
            task_id=_text(data.get("task_id"), 128),
            changed_paths=_items(data.get("changed_paths")),
            artifacts=_items(data.get("artifacts")),
            checks=_mappings(data.get("checks"), ("name", "passed")),
            criteria=_mappings(data.get("criteria"), ("kind", "observed")),
            unresolved=_items(data.get("unresolved"), MAX_TEXT),
            observed_at=_text(data.get("observed_at")),
            contract_version=contract_version,
        )


@dataclass(frozen=True)
class EvidenceOutcome:
    """One r2-evidence-v1 record: the verdict plus how it was reached."""

    decision_id: str
    session_id: str | None
    stage_key: str
    task_id: str
    policy: str
    typed_result: bool
    verification: Verification
    observed_at: str = ""
    contract_version: int = R2_CONTRACT_VERSION
    schema: str = TASK_EVIDENCE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "stage_key": self.stage_key,
            "task_id": self.task_id,
            "policy": self.policy,
            "typed_result": self.typed_result,
            "observed_at": self.observed_at or datetime.now(UTC).isoformat(),
            **self.verification.to_dict(),
        }


def task_evidence_path() -> Path:
    return sidecar_path("r2-evidence-v1.jsonl")


def _generations(path: Path) -> tuple[Path, ...]:
    return tuple(candidate for candidate in sidecar_generations(path)[::-1] if candidate.is_file())


def _records(path: Path) -> Iterator[dict[str, Any]]:
    for generation in _generations(path):
        for _, raw in iter_jsonl(generation):
            yield dict(raw)


def _spec_archive(path: Path) -> Path:
    return path.with_name(path.name + ".specs")


def _compact_spec_archive(path: Path) -> None:
    """Carry the latest contract for each stage across archive rotation."""
    records = list(_records(path))
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("schema") != TASK_SPEC_SCHEMA:
            continue
        identity = (str(record.get("decision_id", "")), str(record.get("stage_key", "")))
        if all(identity):
            latest[identity] = record
    if latest:
        persist._rewrite_jsonl(path, list(latest.values()))


def _append_spec_archive_locked(path: Path, spec: TaskSpec) -> None:
    if path.is_file() and path.stat().st_size >= persist.MAX_LOG_BYTES:
        _compact_spec_archive(path)
    _append_jsonl_locked(path, spec.to_dict())


def attach_task_spec(spec: TaskSpec, path: Path | str | None = None) -> TaskSpec:
    """Persist the first spec atomically and retain it beyond log rotation."""
    target = task_evidence_path() if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            existing = find_task_spec(spec.decision_id, spec.stage_key, path=target)
            if existing is not None:
                return existing
            stamped = spec if spec.created_at else _with_created_at(spec)
            # The authoritative archive is written under the same decision lock;
            # the normal shared stream remains available for existing readers.
            _append_jsonl_locked(target, stamped.to_dict())
            _append_spec_archive_locked(_spec_archive(target), stamped)
            return stamped
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def _with_created_at(spec: TaskSpec) -> TaskSpec:
    return TaskSpec(
        decision_id=spec.decision_id,
        stage_key=spec.stage_key,
        role=spec.role,
        session_id=spec.session_id,
        path_scope=spec.path_scope,
        artifacts=spec.artifacts,
        checks=spec.checks,
        criteria=spec.criteria,
        created_at=datetime.now(UTC).isoformat(),
        contract_version=spec.contract_version,
    )


def find_task_spec(
    decision_id: str, stage_key: str, path: Path | str | None = None
) -> TaskSpec | None:
    """Return the immutable spec attached to a stage attempt, if any."""
    target = task_evidence_path() if path is None else Path(path)
    for raw in _records(_spec_archive(target)):
        if (
            raw.get("schema") == TASK_SPEC_SCHEMA
            and raw.get("decision_id") == decision_id
            and raw.get("stage_key") == stage_key
        ):
            spec = TaskSpec.from_dict(raw)
            if spec is not None:
                return spec
    for raw in _records(target):
        if raw.get("schema") != TASK_SPEC_SCHEMA:
            continue
        if raw.get("decision_id") == decision_id and raw.get("stage_key") == stage_key:
            spec = TaskSpec.from_dict(raw)
            if spec is not None:
                return spec
    return None


def record_task_evidence(outcome: EvidenceOutcome, path: Path | str | None = None) -> None:
    """Append one verification outcome to the versioned sidecar."""
    append_jsonl(task_evidence_path() if path is None else path, outcome.to_dict())


def _json_candidates(text: str) -> Iterator[Any]:
    for match in _FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if block.startswith("{"):
            try:
                yield json.loads(block)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            yield json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return


def parse_task_result(text: str | None) -> TaskResult | None:
    """Extract a typed result from a child's terminal text, or ``None``.

    Legacy children emit prose and produce ``None`` here, which is exactly the
    ``typed_evidence_missing`` case: visible metadata under the legacy policy,
    and an evidence failure once the strict policy is enabled for a route.
    """
    if not text or TASK_RESULT_SCHEMA not in text:
        return None
    for candidate in _json_candidates(text):
        if isinstance(candidate, Mapping):
            result = TaskResult.from_dict(candidate)
            if result is not None:
                return result
    return None


def observe(
    spec: TaskSpec,
    *,
    repo_root: Path | str,
    changed_paths: Sequence[str] = (),
    checks: Mapping[str, bool] | None = None,
) -> Observation:
    """Build the verifier's view of the world from the runtime's own reads.

    Only paths the spec or the runtime already named are probed, so this stays
    bounded and never walks the tree.
    """
    root = Path(repo_root)
    raw_candidates = {*spec.artifacts, *changed_paths}
    for criterion in spec.criteria:
        if criterion.get("kind") in {"path_exists", "path_changed"}:
            raw_candidates.add(str(criterion.get("expected") or ""))
    candidates = {
        canonical
        for candidate in raw_candidates
        if (canonical := _canonical_repo_path(candidate)) is not None
    }
    existing = frozenset(candidate for candidate in candidates if (root / candidate).exists())
    observed_changes = tuple(
        canonical for path in changed_paths if (canonical := _canonical_repo_path(path)) is not None
    )
    return Observation(
        existing_paths=existing,
        changed_paths=observed_changes,
        checks=dict(checks or {}),
    )


def verify_task_result(
    spec: TaskSpec,
    result: TaskResult | None,
    observation: Observation,
) -> Verification:
    """Thin seam over the pure verifier, kept here so callers import one module."""
    return verify(spec, result, observation)


@dataclass(frozen=True)
class EvidenceDecision:
    """How the completion seam should treat one retrieval under a policy."""

    policy: str
    typed_result: bool
    verification: Verification
    completes: bool
    legacy_success: bool = False
    marker: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def decide_completion(
    *,
    policy: str | None,
    legacy_success: bool,
    spec: TaskSpec | None,
    result: TaskResult | None,
    observation: Observation,
) -> EvidenceDecision:
    """Decide whether a retrieval completes a stage under the active policy.

    Under the legacy policy the answer is exactly today's answer, and the typed
    layer only records what it saw. Under the strict policy a stage completes
    only when an attached spec exists and the runtime verified the result
    against it; a terminal result with no typed payload cannot complete it.
    """
    active = (policy or "legacy").strip().casefold() or "legacy"
    typed = result is not None
    marker = "" if typed else "typed_evidence_missing"
    if active != "strict":
        # Under the legacy policy the runtime does not gather the observation a
        # verdict would need, so claiming one would be worse than none: record
        # what was seen and leave the decision exactly where it is today.
        return EvidenceDecision(
            policy=active,
            typed_result=typed,
            verification=Verification(False, ("not_evaluated",), ()),
            completes=legacy_success,
            legacy_success=legacy_success,
            marker=marker,
            notes=("legacy policy: evidence is recorded, not enforced",),
        )
    if spec is None:
        verification = Verification(
            False, ("result_missing",), ("no task spec was attached to this stage",)
        )
    else:
        verification = verify_task_result(spec, result, observation)
    return EvidenceDecision(
        policy=active,
        typed_result=typed,
        verification=verification,
        completes=bool(verification.verified),
        legacy_success=legacy_success,
        marker=marker,
        notes=(
            ()
            if verification.verified
            else ("strict policy: only runtime-verified evidence completes a stage",)
        ),
    )


__all__ = [
    "EvidenceDecision",
    "EvidenceOutcome",
    "MAX_ITEMS",
    "Observation",
    "R2_CONTRACT_VERSION",
    "TASK_EVIDENCE_SCHEMA",
    "TASK_RESULT_SCHEMA",
    "TASK_SPEC_SCHEMA",
    "TaskResult",
    "TaskSpec",
    "Verification",
    "attach_task_spec",
    "decide_completion",
    "find_task_spec",
    "observe",
    "parse_task_result",
    "record_task_evidence",
    "task_evidence_path",
    "verify_task_result",
]
