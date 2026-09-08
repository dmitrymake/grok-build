"""Typed, non-overridable deterministic eligibility gate for artifact selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

GateKind = Literal["tests", "invariant", "schema", "scanner"]
GateStatus = Literal["PASS", "FAIL", "UNKNOWN"]
_GATE_KINDS = frozenset({"tests", "invariant", "schema", "scanner"})
_GATE_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN"})


@dataclass(frozen=True)
class GateCheck:
    kind: GateKind
    name: str
    status: GateStatus
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _GATE_KINDS:
            raise ValueError(f"unsupported gate kind: {self.kind}")
        if self.status not in _GATE_STATUSES:
            raise ValueError(f"unsupported gate status: {self.status}")
        if not self.name:
            raise ValueError("gate check name is required")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class CandidateGate:
    label: str
    status: GateStatus
    checks: tuple[GateCheck, ...]

    @property
    def eligible(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class GateDecision:
    candidates: tuple[CandidateGate, ...]
    eligible: tuple[str, ...]
    rejected: tuple[str, ...]
    unknown: tuple[str, ...]
    outcome: Literal["proceed", "select_survivor", "reject_all", "abstain"]

    def status_for(self, label: str) -> GateStatus:
        return next(item.status for item in self.candidates if item.label == label)


def decide_candidate(label: str, checks: Iterable[GateCheck]) -> CandidateGate:
    frozen = tuple(checks)
    if any(check.status == "FAIL" for check in frozen):
        status: GateStatus = "FAIL"
    elif not frozen or any(check.status == "UNKNOWN" for check in frozen):
        status = "UNKNOWN"
    else:
        status = "PASS"
    return CandidateGate(label, status, frozen)


def decide_gate(candidates: Mapping[str, Iterable[GateCheck]]) -> GateDecision:
    decisions = tuple(decide_candidate(label, checks) for label, checks in candidates.items())
    eligible = tuple(item.label for item in decisions if item.status == "PASS")
    rejected = tuple(item.label for item in decisions if item.status == "FAIL")
    unknown = tuple(item.label for item in decisions if item.status == "UNKNOWN")
    if unknown:
        outcome = "abstain"
    elif not eligible:
        outcome = "reject_all"
    elif len(eligible) == 1:
        outcome = "select_survivor"
    else:
        outcome = "proceed"
    return GateDecision(decisions, eligible, rejected, unknown, outcome)


def legacy_checks(
    required: Iterable[str], results: Mapping[str, bool | None], *, kind: GateKind = "tests"
) -> tuple[GateCheck, ...]:
    """Adapt legacy boolean hard results without treating absence as a pass."""
    return tuple(
        GateCheck(
            kind,
            name,
            "PASS"
            if results.get(name) is True
            else "FAIL"
            if results.get(name) is False
            else "UNKNOWN",
        )
        for name in required
    )


__all__ = [
    "CandidateGate",
    "GateCheck",
    "GateDecision",
    "GateKind",
    "GateStatus",
    "decide_candidate",
    "decide_gate",
    "legacy_checks",
]
