#!/usr/bin/env python3
"""Pure per-criterion verdict and escalation-contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

from grokbuild.criterion_judge import (  # noqa: E402
    CALIBRATION_TOP_LOGPROBS,
    judge_criterion,
    judge_criterion_token,
)
from grokbuild.task_verify import Observation  # noqa: E402


@dataclass(frozen=True)
class Criterion:
    kind: str
    expected: str


def test_clear_evidence_passes_each_supported_criterion_kind() -> None:
    evidence = Observation(
        existing_paths=frozenset({"src/app.py"}),
        changed_paths=("src/app.py",),
        checks={"tests": True},
    )

    assert judge_criterion({"kind": "path_exists", "expected": "src/app.py"}, evidence) == "pass"
    assert judge_criterion(Criterion("path_changed", "src/app.py"), evidence) == "pass"
    assert judge_criterion({"kind": "check_passed", "expected": "tests"}, evidence) == "pass"


def test_clear_violations_fail_each_supported_criterion_kind() -> None:
    evidence = {
        "existing_paths": frozenset(),
        "changed_paths": (),
        "checks": {"tests": False},
    }

    assert judge_criterion({"kind": "path_exists", "expected": "src/app.py"}, evidence) == "fail"
    assert judge_criterion({"kind": "path_changed", "expected": "src/app.py"}, evidence) == "fail"
    assert judge_criterion({"kind": "check_passed", "expected": "tests"}, evidence) == "fail"


def test_absent_check_outcome_is_disputed() -> None:
    criterion = {"kind": "check_passed", "expected": "tests"}
    assert judge_criterion(criterion, Observation(checks={})) == "unknown"


def test_unknown_criterion_kind_is_disputed() -> None:
    criterion = {"kind": "semantic_quality", "expected": "clear"}
    assert judge_criterion(criterion, Observation()) == "unknown"


def test_contradictory_check_evidence_is_disputed() -> None:
    criterion = {"kind": "check_passed", "expected": "tests"}
    evidence = {"checks": {"tests": (True, False)}}
    assert judge_criterion(criterion, evidence) == "unknown"


def test_malformed_criterion_is_disputed() -> None:
    assert judge_criterion({"kind": "path_exists"}, Observation()) == "unknown"


def test_missing_path_probe_is_disputed() -> None:
    criterion = {"kind": "path_exists", "expected": "src/app.py"}
    assert judge_criterion(criterion, {"checks": {}}) == "unknown"


def test_settled_evidence_never_escalates() -> None:
    cases = (
        ({"kind": "path_exists", "expected": "present"}, {"existing_paths": {"present"}}),
        ({"kind": "path_exists", "expected": "missing"}, {"existing_paths": set()}),
        ({"kind": "path_changed", "expected": "changed"}, {"changed_paths": ("changed",)}),
        ({"kind": "path_changed", "expected": "unchanged"}, {"changed_paths": ()}),
        ({"kind": "check_passed", "expected": "ok"}, {"checks": {"ok": True}}),
        ({"kind": "check_passed", "expected": "bad"}, {"checks": {"bad": False}}),
    )

    assert all(judge_criterion(criterion, evidence) != "unknown" for criterion, evidence in cases)


def test_forced_token_requires_real_provider_logits_and_calibrated_margin() -> None:
    assert judge_criterion_token("PASS", None, calibrated_threshold=0.5).outcome == "ABSTAIN_LOGITS_UNAVAILABLE"
    assert judge_criterion_token(
        "PASS", {"PASS": -0.1, "FAIL": -2.0, "ABSTAIN": -3.0}, calibrated_threshold=0.5
    ).outcome == "PASS"
    assert judge_criterion_token(
        "FAIL", {"PASS": -0.2, "FAIL": -0.1, "ABSTAIN": -2.0}, calibrated_threshold=0.5
    ).outcome == "ABSTAIN"
    assert judge_criterion_token("yes", {"PASS": 0.0, "FAIL": -1.0}, calibrated_threshold=0.1).outcome == "ABSTAIN_MALFORMED_TOKEN"


def test_non_finite_scores_and_thresholds_abstain_uncalibrated() -> None:
    for score in (float("nan"), float("inf"), float("-inf")):
        verdict = judge_criterion_token(
            "PASS", {"PASS": score, "FAIL": -1.0}, calibrated_threshold=0.1
        )
        assert verdict.outcome == "ABSTAIN_LOGITS_UNAVAILABLE"
        assert not verdict.calibrated
    for threshold in (float("nan"), float("inf"), float("-inf"), -0.1):
        verdict = judge_criterion_token(
            "PASS", {"PASS": -0.1, "FAIL": -1.0}, calibrated_threshold=threshold
        )
        assert verdict.outcome == "ABSTAIN_LOGITS_UNAVAILABLE"
        assert not verdict.calibrated


def test_token_alias_log_probabilities_are_combined() -> None:
    verdict = judge_criterion_token(
        "PASS",
        {"PASS": -1.0, " PASS": -1.0, "FAIL": -0.5},
        calibrated_threshold=0.1,
    )
    assert verdict.outcome == "PASS"
    assert verdict.margin is not None and verdict.margin > 0.1


def test_verdict_is_deterministic() -> None:
    criterion = {"kind": "check_passed", "expected": "tests"}
    evidence = {"checks": {"tests": True}}

    assert {judge_criterion(criterion, evidence) for _ in range(100)} == {"pass"}


def test_calibration_window_covers_at_least_two_outcomes():
    assert CALIBRATION_TOP_LOGPROBS == 20
    assert judge_criterion_token(
        "PASS", {"PASS": -0.1, "FAIL": -0.35}, calibrated_threshold=0.2
    ).outcome == "PASS"
    assert judge_criterion_token(
        "PASS", {" PASS\n": -0.1, "ĠFAIL": -0.35}, calibrated_threshold=0.2
    ).outcome == "PASS"
    assert judge_criterion_token(
        "PASS", {"PASS": -0.1}, calibrated_threshold=0.2
    ).outcome == "ABSTAIN_LOGITS_UNAVAILABLE"
