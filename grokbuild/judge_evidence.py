"""Canonical evidence-only input boundary for semantic artifact judges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

FORBIDDEN_EVIDENCE_FIELDS = frozenset(
    {
        "reference",
        "reference_solution",
        "expected_patch",
        "answer_key",
        "rubric_answer_key",
        "hidden_verifier",
        "verifier_source",
        "raw_assistant_response",
        "response",
        "trajectory",
        "chain_of_thought",
        "reasoning",
        "prior_judge_rationale",
        "judge_rationale",
        "model",
        "provider",
        "author",
        "agent",
    }
)


@dataclass(frozen=True)
class EvidenceFact:
    kind: str
    name: str
    status: str
    evidence_ref: str = ""


@dataclass(frozen=True)
class JudgeEvidenceBundle:
    label: str
    artifacts: tuple[tuple[str, str], ...]
    facts: tuple[EvidenceFact, ...] = ()
    traces: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = 1


def forbidden_fields(payload: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(key)
            for key in payload
            if str(key).casefold().replace("-", "_") in FORBIDDEN_EVIDENCE_FIELDS
        )
    )


def bounded_traces(values: Sequence[str], *, limit: int = 16, width: int = 500) -> tuple[str, ...]:
    return tuple(str(value)[:width] for value in values[:limit])


__all__ = [
    "EvidenceFact",
    "FORBIDDEN_EVIDENCE_FIELDS",
    "JudgeEvidenceBundle",
    "bounded_traces",
    "forbidden_fields",
]
