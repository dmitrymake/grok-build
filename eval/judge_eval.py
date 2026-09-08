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
    complete: bool = True
    incomplete_reason: str = ""


def evaluate_canaries(
    battery_version: str,
    expected: Mapping[str, str],
    observed: Mapping[str, Sequence[str]],
    aggregates: Sequence[ComparisonAggregate] = (),
    *,
    planned_repetitions: int = MIN_REPETITIONS,
) -> JudgeEvalResult:
    """Score frozen cases; perfect repeated success is an alarm, not promotion evidence."""
    complete = (
        bool(expected)
        and isinstance(planned_repetitions, int)
        and not isinstance(planned_repetitions, bool)
        and planned_repetitions >= MIN_REPETITIONS
        and set(observed) == set(expected)
        and all(
            isinstance(answers, Sequence)
            and not isinstance(answers, (str, bytes))
            and len(answers) == planned_repetitions
            for answers in observed.values()
        )
    )
    repetitions = planned_repetitions if complete else 0
    case_scores = (
        [
            sum(answer == expected[case_id] for answer in observed[case_id]) / planned_repetitions
            for case_id in expected
        ]
        if complete
        else []
    )
    score = sum(case_scores) / len(case_scores) if case_scores else 0.0
    contamination = complete and score == 1.0
    repeatability = (
        sum(item.repeatability for item in aggregates) / len(aggregates) if aggregates else 0.0
    )
    available_bias = [item.position_bias for item in aggregates if item.position_bias is not None]
    position_bias = sum(available_bias) / len(available_bias) if available_bias else 0.0
    return JudgeEvalResult(
        battery_version,
        score,
        repeatability,
        position_bias,
        contamination,
        repetitions,
        complete,
        "" if complete else "case coverage or planned repetition count is incomplete",
    )


__all__ = ["JudgeEvalResult", "MIN_REPETITIONS", "evaluate_canaries"]
