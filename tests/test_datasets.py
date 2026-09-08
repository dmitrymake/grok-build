from __future__ import annotations

import pytest

from grokbuild.datasets import (
    MAX_COMPARISON_AGGREGATE_BYTES,
    JudgeCalibrationRecord,
)


def test_comparison_aggregate_serialization_is_explicitly_bounded():
    accepted = JudgeCalibrationRecord(
        task_id="task",
        candidates=("A", "B"),
        verdict="a",
        comparison_aggregate={"summary": "small"},
    ).to_dict()
    assert accepted["comparison_aggregate"] == {"summary": "small"}

    oversized = JudgeCalibrationRecord(
        task_id="task",
        candidates=("A", "B"),
        verdict="a",
        comparison_aggregate={"payload": "x" * MAX_COMPARISON_AGGREGATE_BYTES},
    )
    with pytest.raises(ValueError, match="serialized size limit"):
        oversized.to_dict()


def test_comparison_aggregate_must_be_json_serializable():
    record = JudgeCalibrationRecord(
        task_id="task",
        candidates=("A", "B"),
        verdict="a",
        comparison_aggregate={"payload": object()},
    )
    with pytest.raises(ValueError, match="JSON serializable"):
        record.to_dict()
