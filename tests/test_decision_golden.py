"""Byte-for-byte golden coverage for decision-log schema v4."""

import json
from pathlib import Path
from grokbuild.decision import SCHEMA_VERSION, decision_from_dict, decision_to_dict

FIXTURE = Path(__file__).parent / "fixtures" / "decision-v4-golden.json"


def test_decision_v4_golden_is_byte_identical():
    expected = FIXTURE.read_text(encoding="utf-8")
    decision = decision_from_dict(json.loads(expected))
    assert json.dumps(decision_to_dict(decision), ensure_ascii=False, indent=2) + "\n" == expected


def test_judge_v2_records_do_not_churn_route_decision_v4():
    assert SCHEMA_VERSION == 4
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "judge_health_snapshot" not in expected
    assert "comparison_aggregates" not in expected
