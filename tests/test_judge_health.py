from grokbuild.judge_health import (
    JudgeHealthRecord,
    delta_below_instrument_noise,
    read_judge_health,
    record_judge_health,
)


def record():
    return JudgeHealthRecord(
        "h1", "provider", "https://endpoint.invalid", "requested", "snapshot",
        "a" * 64, "rubric-v1", 0.0, "high", "judge-opinion-v2",
        "judge-canaries-v1", 0.92, 0.04,
    )


def test_complete_health_record_round_trips_separately(tmp_path):
    path = tmp_path / "health.jsonl"
    record_judge_health(record(), path)
    stored = list(read_judge_health(path))
    assert stored[0]["resolved_model"] == "snapshot"
    assert "credential" not in stored[0]


def test_selector_delta_below_repeatability_noise_is_flagged():
    assert delta_below_instrument_noise(0.05, (record(),))
    assert not delta_below_instrument_noise(0.2, (record(),))
