"""The datasets the control plane is worth building: escalation, judging, and evidence.

Neither is a training set yet. They exist so that when there is enough recorded
history to fit a routing policy against, the history is already there, bounded,
redacted and versioned - rather than being reconstructed after the fact from
logs that were never designed to answer the question.

Verification-plan and attribution sidecars are collection-only: AWAITING
CALIBRATION DATASET. They do not train a model or supply calibration thresholds.
Probe (Qwen3-style hidden-states) and JudgePanel-14B remain watchlist items - not
bindings; inclusion requires the identity-leak/position-bias calibrator first.

Two rules shape what may be written:

* **Provider identity is never a quality feature.** A dataset that records which
  vendor produced the accepted candidate teaches a router to prefer a brand,
  which is exactly the bias the artifact judge exists to remove. Candidates are
  recorded by their anonymous comparison label only, and any attempt to record a
  model or provider is dropped at the boundary.
* **Records stay bounded.** Every write goes through the shared appender, so the
  files rotate at the same size cap as every other sidecar, and the per-record
  field caps keep one pathological entry from crowding out a thousand useful
  ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from grokbuild.persist import append_jsonl, iter_jsonl, sidecar_generations, sidecar_path

DATASET_CONTRACT_VERSION = 1
ESCALATION_SCHEMA = "escalation-eval-v1"
JUDGE_CALIBRATION_SCHEMA = "judge-calibration-v1"
VERIFICATION_PLAN_SCHEMA = "verification-plan-v1"
ATTRIBUTION_SCHEMA = "attribution-v1"

MAX_ITEMS = 32
MAX_TEXT = 200

# Keys that would tie a record to a vendor rather than to the work. They are
# refused at construction, not filtered later, so a caller cannot smuggle one in
# by nesting it.
FORBIDDEN_KEYS = frozenset(
    {"agent", "author", "credential", "endpoint", "model", "provider", "vendor"}
)


class ProviderIdentityRefused(ValueError):
    """Raised when a dataset record would record who produced a result."""


def _text(value: object, limit: int = MAX_TEXT) -> str:
    return str(value or "")[:limit]


def _items(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return ()
    return tuple(_text(value) for value in values[:MAX_ITEMS])


def _reject_identity(payload: Any, path: str = "") -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key).casefold()
            if name in FORBIDDEN_KEYS:
                raise ProviderIdentityRefused(
                    f"{path or 'record'}.{key}: provider identity is not a quality feature"
                )
            _reject_identity(value, f"{path}.{key}" if path else str(key))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for index, value in enumerate(payload):
            _reject_identity(value, f"{path}[{index}]")


def dataset_path(schema: str) -> Path:
    return sidecar_path(f"{schema}.jsonl")


def _generations(path: Path) -> tuple[Path, ...]:
    return tuple(candidate for candidate in sidecar_generations(path)[::-1] if candidate.is_file())


def read_records(schema: str, path: Path | str | None = None) -> Iterator[dict[str, Any]]:
    """Read every generation oldest-first, so a rotation does not hide history."""
    target = dataset_path(schema) if path is None else Path(path)
    for generation in _generations(target):
        for _, raw in iter_jsonl(generation):
            if raw.get("schema") == schema:
                yield dict(raw)


@dataclass(frozen=True)
class EscalationRecord:
    """One escalation decision and, later, what it turned out to be worth.

    ``outcome`` is filled in after the fact: whether the cheap path would have
    sufficed (a false positive) or failed where the frontier would have caught
    it (a false negative). Until real outcomes accumulate this is a collection
    mechanism, not a model - AWAITING ESCALATION DATASET.
    """

    decision_id: str
    task_class: str
    complexity: str
    signals: Mapping[str, float]
    score: float
    threshold: float
    escalated: bool
    candidate_supply: float | None = None
    failure_kind: str | None = None
    outcome: str = "unknown"
    notes: tuple[str, ...] = ()
    observed_at: str = ""
    contract_version: int = DATASET_CONTRACT_VERSION
    schema: str = ESCALATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "decision_id": _text(self.decision_id, 128),
            "task_class": _text(self.task_class, 64),
            "complexity": _text(self.complexity, 32),
            "signals": {str(key): float(value) for key, value in self.signals.items()},
            "score": float(self.score),
            "threshold": float(self.threshold),
            "escalated": bool(self.escalated),
            "candidate_supply": (
                None if self.candidate_supply is None else float(self.candidate_supply)
            ),
            "failure_kind": None if self.failure_kind is None else _text(self.failure_kind, 32),
            "outcome": _text(self.outcome, 32),
            "notes": list(_items(self.notes)),
            "observed_at": self.observed_at or datetime.now(UTC).isoformat(),
        }
        _reject_identity(payload)
        return payload


@dataclass(frozen=True)
class JudgeCalibrationRecord:
    """One comparison, by anonymous label, with what happened to the winner."""

    task_id: str
    candidates: tuple[str, ...]
    verdict: str
    reason_codes: tuple[str, ...] = ()
    hard_failures: tuple[str, ...] = ()
    accepted: str = ""
    later_regression: bool | None = None
    human_correction: str = ""
    comparison_aggregate: Mapping[str, Any] | None = None
    health_id: str = ""
    observed_at: str = ""
    contract_version: int = DATASET_CONTRACT_VERSION
    schema: str = JUDGE_CALIBRATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "task_id": _text(self.task_id, 128),
            "candidates": list(_items(self.candidates)),
            "verdict": _text(self.verdict, 48),
            "reason_codes": list(_items(self.reason_codes)),
            "hard_failures": list(_items(self.hard_failures)),
            "accepted": _text(self.accepted, 48),
            "later_regression": self.later_regression,
            "human_correction": _text(self.human_correction),
            "comparison_aggregate": (
                dict(self.comparison_aggregate) if self.comparison_aggregate is not None else None
            ),
            "health_id": _text(self.health_id, 128),
            "observed_at": self.observed_at or datetime.now(UTC).isoformat(),
        }
        _reject_identity(payload)
        return payload


def record_escalation(record: EscalationRecord, path: Path | str | None = None) -> None:
    """Append one escalation decision to the bounded, redacted dataset."""
    payload = record.to_dict()
    append_jsonl(dataset_path(record.schema) if path is None else path, payload)


def record_judge_calibration(
    record: JudgeCalibrationRecord, path: Path | str | None = None
) -> None:
    """Append one anonymised comparison outcome to the calibration dataset."""
    payload = record.to_dict()
    append_jsonl(dataset_path(record.schema) if path is None else path, payload)


@dataclass(frozen=True)
class VerificationPlanRecord:
    """A bounded, anonymous request for evidence collection."""

    task_id: str
    candidate_labels: tuple[str, ...]
    kind: str
    hypothesis: str
    constraints: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    affected_path_count: int = 0
    invariant_tags: tuple[str, ...] = ()
    decision_id: str = ""
    task_class: str = ""
    observed_at: str = ""
    contract_version: int = DATASET_CONTRACT_VERSION
    schema: str = VERIFICATION_PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        _reject_identity(vars(self))
        payload = {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "task_id": _text(self.task_id, 128),
            "candidate_labels": list(_items(self.candidate_labels)),
            "kind": _text(self.kind, MAX_TEXT),
            "hypothesis": _text(self.hypothesis),
            "constraints": list(_items(self.constraints)),
            "requirements": list(_items(self.requirements)),
            "checks": list(_items(self.checks)),
            "affected_path_count": max(0, min(int(self.affected_path_count), MAX_ITEMS)),
            "invariant_tags": list(_items(self.invariant_tags)),
            "decision_id": _text(self.decision_id, 128),
            "task_class": _text(self.task_class, 64),
            "observed_at": self.observed_at or datetime.now(UTC).isoformat(),
        }
        _reject_identity(payload)
        return payload


def record_verification_plan(
    record: VerificationPlanRecord, path: Path | str | None = None
) -> None:
    """Append one collection-only verification plan."""
    payload = record.to_dict()
    append_jsonl(dataset_path(record.schema) if path is None else path, payload)


@dataclass(frozen=True)
class AttributionRecord:
    """One anonymous attribution outcome from a discriminating check."""

    task_id: str
    baseline_label: str
    candidate_label: str
    outcome: str
    affected_behavior_tags: tuple[str, ...] = ()
    discriminating_check: str = ""
    decision_id: str = ""
    task_class: str = ""
    observed_at: str = ""
    contract_version: int = DATASET_CONTRACT_VERSION
    schema: str = ATTRIBUTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        _reject_identity(vars(self))
        payload = {
            "schema": self.schema,
            "contract_version": self.contract_version,
            "task_id": _text(self.task_id, 128),
            "baseline_label": _text(self.baseline_label, 48),
            "candidate_label": _text(self.candidate_label, 48),
            "outcome": _text(self.outcome, 32),
            "affected_behavior_tags": list(_items(self.affected_behavior_tags)),
            "discriminating_check": _text(self.discriminating_check),
            "decision_id": _text(self.decision_id, 128),
            "task_class": _text(self.task_class, 64),
            "observed_at": self.observed_at or datetime.now(UTC).isoformat(),
        }
        _reject_identity(payload)
        return payload


def record_attribution(record: AttributionRecord, path: Path | str | None = None) -> None:
    """Append one collection-only attribution outcome."""
    payload = record.to_dict()
    append_jsonl(dataset_path(record.schema) if path is None else path, payload)


def escalation_summary(path: Path | str | None = None) -> dict[str, Any]:
    """Count what the dataset can and cannot yet answer.

    Reporting how many outcomes are still unknown is the point: a false-positive
    rate computed over three labelled rows would look like a measurement while
    being noise.
    """
    total = 0
    escalated = 0
    outcomes: dict[str, int] = {}
    for record in read_records(ESCALATION_SCHEMA, path):
        total += 1
        escalated += bool(record.get("escalated"))
        key = str(record.get("outcome") or "unknown")
        outcomes[key] = outcomes.get(key, 0) + 1
    labelled = total - outcomes.get("unknown", 0)
    return {
        "schema": ESCALATION_SCHEMA,
        "contract_version": DATASET_CONTRACT_VERSION,
        "records": total,
        "escalated": escalated,
        "outcomes": dict(sorted(outcomes.items())),
        "labelled": labelled,
        "status": "awaiting escalation dataset" if labelled < 100 else "labelled",
    }


__all__ = [
    "DATASET_CONTRACT_VERSION",
    "ESCALATION_SCHEMA",
    "JUDGE_CALIBRATION_SCHEMA",
    "VERIFICATION_PLAN_SCHEMA",
    "ATTRIBUTION_SCHEMA",
    "EscalationRecord",
    "JudgeCalibrationRecord",
    "VerificationPlanRecord",
    "AttributionRecord",
    "ProviderIdentityRefused",
    "dataset_path",
    "escalation_summary",
    "read_records",
    "record_escalation",
    "record_judge_calibration",
    "record_verification_plan",
    "record_attribution",
]
