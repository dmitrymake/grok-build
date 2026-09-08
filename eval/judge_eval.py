"""Frozen judge canary scoring and contamination signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from grokbuild.artifact_judge import ComparisonAggregate

MIN_REPETITIONS = 3


@dataclass(frozen=True)
class JudgeEvalResult:
    battery_version: str
    score: float
    repeatability: float
    position_bias: float
    contamination_suspected: bool
    repetitions: int


def evaluate_canaries(
    battery_version: str,
    expected: Mapping[str, str],
    observed: Mapping[str, Sequence[str]],
    aggregates: Sequence[ComparisonAggregate] = (),
) -> JudgeEvalResult:
    """Score frozen cases; perfect repeated success is an alarm, not promotion evidence."""
    runs = [
        answer == expected[case_id]
        for case_id, answers in observed.items()
        if case_id in expected
        for answer in answers
    ]
    repetitions = min((len(observed.get(case_id, ())) for case_id in expected), default=0)
    score = sum(runs) / len(runs) if runs else 0.0
    contamination = repetitions >= MIN_REPETITIONS and bool(runs) and all(runs)
    repeatability = (
        sum(item.repeatability for item in aggregates) / len(aggregates) if aggregates else 0.0
    )
    position_bias = (
        sum(item.position_bias for item in aggregates) / len(aggregates) if aggregates else 0.0
    )
    return JudgeEvalResult(
        battery_version,
        score,
        repeatability,
        position_bias,
        contamination,
        repetitions,
    )


__all__ = ["JudgeEvalResult", "MIN_REPETITIONS", "evaluate_canaries"]
