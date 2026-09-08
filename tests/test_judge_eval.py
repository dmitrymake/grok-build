from eval.judge_eval import evaluate_canaries
from grokbuild.compose import JUDGE_PANEL, compose_judge_challengers
from grokbuild.policy import load_profiles
from grokbuild.roles import load_registry


def test_repeated_perfect_adversarial_canaries_signal_contamination():
    result = evaluate_canaries("v1", {"impossible": "FAIL"}, {"impossible": ["FAIL"] * 3})
    assert result.score == 1.0
    assert result.contamination_suspected


def test_canary_scoring_is_incomplete_without_full_planned_coverage():
    missing_case = evaluate_canaries("v1", {"one": "PASS", "two": "FAIL"}, {"one": ["PASS"] * 3})
    assert not missing_case.complete
    assert missing_case.score == 0.0
    wrong_repetitions = evaluate_canaries(
        "v1", {"one": "PASS"}, {"one": ["PASS"] * 2}, planned_repetitions=3
    )
    assert not wrong_repetitions.complete
    assert not wrong_repetitions.contamination_suspected


def test_canary_cases_receive_equal_weight_after_complete_repetitions():
    result = evaluate_canaries(
        "v1",
        {"one": "PASS", "two": "FAIL"},
        {"one": ["PASS", "PASS", "PASS"], "two": ["FAIL", "PASS", "PASS"]},
    )
    assert result.complete
    assert result.score == (1.0 + (1 / 3)) / 2


def test_challengers_are_opt_in_and_default_is_unchanged():
    profiles = load_profiles()
    assert compose_judge_challengers(profiles["default"]) == []
    assert [name for name, _ in JUDGE_PANEL] == ["judge-primary", "judge-independent"]


def test_challenger_fixture_without_endpoints_remains_unavailable():
    profiles = load_profiles()
    registry = load_registry()
    without_challengers = {
        model: meta
        for model, meta in registry.provider_catalog.items()
        if model not in {"gpt-oss-120b", "gpt-oss-20b", "qwen-qwq-32b"}
    }
    fixture_registry = registry.with_provider_catalog(without_challengers)
    assert compose_judge_challengers(profiles["judge-challengers"], registry=fixture_registry) == []


def test_bound_challengers_compose_from_live_registry():
    profiles = load_profiles()
    stages = compose_judge_challengers(profiles["judge-challengers"], registry=load_registry())
    assert len(stages) == 1
    assert [member.role for member in stages[0].members] == [
        "judge-challenger-agentic",
        "judge-challenger-structural",
    ]
    registry = load_registry()
    assert registry.get("judge-challenger-agentic").model == "gpt-oss-120b"
    assert registry.get("judge-challenger-agentic").reasoning_effort == "low"
    assert registry.get("judge-challenger-structural").model == "gpt-oss-120b"
    assert registry.get("judge-challenger-structural").reasoning_effort == "low"
