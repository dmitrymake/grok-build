from __future__ import annotations

import json

import pytest

from grokbuild import gate, settlement
from grokbuild.decision import ExecutionStage
from grokbuild.evidence import FailureSignal
from grokbuild.state import ExecutionTrack, RuntimeState, load_state


def test_plain_rate_limit_message_is_a_quota_signal() -> None:
    assert settlement._quota_signal(
        FailureSignal(text="Retry failed: Rate limit reached for requests")
    )


def _state(tmp_path, stages=()):
    path = tmp_path / "state.json"
    track = ExecutionTrack("decision-1", "session-1", 1, stages=stages)
    track.requested.append("review")
    state = RuntimeState(executions={"decision-1": track}, source_path=path)
    path.write_text(json.dumps(state.to_dict()))
    return path, state


def test_reactive_failover_records_reserve_and_recipe(monkeypatch, tmp_path):
    path, state = _state(tmp_path)
    logs = []
    monkeypatch.setattr(settlement, "default_state_path", lambda: path)
    monkeypatch.setattr(settlement.roles, "load_registry", lambda: object())

    def available(*args, **kwargs):
        kwargs["endpoint_events"].append({"provider": "commandcode"})
        return True, None

    monkeypatch.setattr(settlement, "_role_availability", available)
    monkeypatch.setattr(settlement, "_append_log", lambda record, state=None: logs.append(record))
    result = settlement._reactive_failover(
        {},
        {"decision_id": "decision-1", "session_id": "session-1", "_runtime_state": state},
        "review",
        "review",
        {"provider": "opencode", "unavailable_until": 9999999999.0},
    )
    assert result == {"from_provider": "opencode", "to_provider": "commandcode", "role": "review"}
    saved = load_state(path).get_execution("decision-1")
    assert saved is not None and saved.reactive_respawns["review"]["to_provider"] == "commandcode"
    recipe = gate._stage_recipe({"intent": "review"}, "review", saved)
    assert "primary opencode quota-held" in recipe
    assert "respawn review" in recipe
    assert "reserve commandcode" in recipe
    saved.reactive_respawns["review"]["unavailable_until"] = 0.0
    assert "reserve commandcode" not in gate._stage_recipe({"intent": "review"}, "review", saved)
    assert logs[0]["event"] == "reactive_failover"
    assert logs[0]["decision_id"] == "decision-1"
    assert logs[0]["from_provider"] == "opencode"
    assert logs[0]["to_provider"] == "commandcode"


def test_post_tool_failure_linear_stage_records_reactive_failover(monkeypatch, tmp_path):
    stage = ExecutionStage("review-hard", True, "review the patch")
    path, state = _state(tmp_path, stages=(stage,))
    logs = []
    monkeypatch.setattr(settlement, "default_state_path", lambda: path)
    monkeypatch.setattr(settlement, "_session_id", lambda data: data.get("sessionId"))
    monkeypatch.setattr(settlement, "_spawn_subagent_type", lambda data: "review")
    monkeypatch.setattr(
        settlement,
        "_failure_signal",
        lambda data, context="": settlement.FailureSignal(reason="429 quota"),
    )
    monkeypatch.setattr(
        settlement,
        "resolve_route",
        lambda *args, **kwargs: {
            "decision_id": "decision-1",
            "session_id": "session-1",
            "mode": "dynamic",
            "intent": "review",
            "_runtime_state": state,
        },
    )
    monkeypatch.setattr(settlement.roles, "load_registry", lambda: object())
    monkeypatch.setattr(settlement, "_circuit_params", lambda route: (3, 300.0))
    monkeypatch.setattr(settlement, "_consilium_threshold", lambda route: 3)
    monkeypatch.setattr(settlement, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        settlement, "_load_execution", lambda decision_id: state.get_execution(decision_id)
    )
    monkeypatch.setattr(
        settlement,
        "_maybe_open_quota_circuit",
        lambda *args, **kwargs: {
            "provider": "opencode",
            "unavailable_until": 9999999999.0,
            "reason": "quota/429",
        },
    )

    def available(*args, **kwargs):
        kwargs["endpoint_events"].append({"provider": "commandcode"})
        return True, None

    monkeypatch.setattr(settlement, "_role_availability", available)
    monkeypatch.setattr(settlement, "_append_log", lambda record, state=None: logs.append(record))

    settlement.handle_post_tool_failure(
        {
            "sessionId": "session-1",
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
            "toolResult": {"status": 429, "error": "quota exhausted"},
        },
        {},
    )

    saved = load_state(path).get_execution("decision-1")
    assert saved is not None
    assert saved.reactive_respawns["review-hard"]["to_provider"] == "commandcode"
    assert any(record["event"] == "reactive_failover" for record in logs)
    recipe = gate._stage_recipe({"intent": "review"}, "review-hard", saved)
    assert "reserve commandcode" in recipe


@pytest.mark.parametrize(
    "reason",
    ["single-endpoint", "reserve circuit-held", "reserve quota pressured", "credential missing"],
)
def test_no_reserve_does_not_mark_or_log(monkeypatch, tmp_path, reason):
    path, state = _state(tmp_path)
    logs = []
    monkeypatch.setattr(settlement, "default_state_path", lambda: path)
    monkeypatch.setattr(settlement.roles, "load_registry", lambda: object())
    monkeypatch.setattr(settlement, "_role_availability", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(settlement, "_append_log", lambda record, state=None: logs.append(record))
    assert (
        settlement._reactive_failover(
            {},
            {"decision_id": "decision-1", "_runtime_state": state},
            "review",
            "review",
            {"provider": "opencode", "unavailable_until": 9999999999.0},
        )
        is None
    )
    saved = load_state(path).get_execution("decision-1")
    assert saved is not None and not saved.reactive_respawns
    assert not logs


def test_reactive_failover_is_bounded(monkeypatch, tmp_path):
    path, state = _state(tmp_path)
    state.executions["decision-1"].reactive_respawns["review"] = {
        "count": 1,
        "from_provider": "opencode",
        "to_provider": "commandcode",
        "role": "review",
    }
    monkeypatch.setattr(settlement, "default_state_path", lambda: path)
    monkeypatch.setattr(settlement.roles, "load_registry", lambda: object())
    assert (
        settlement._reactive_failover(
            {},
            {"decision_id": "decision-1", "_runtime_state": state},
            "review",
            "review",
            {"provider": "commandcode"},
        )
        is None
    )


def test_primary_then_reserve_failure_has_no_second_reactive_retry(monkeypatch, tmp_path):
    path, state = _state(tmp_path)
    logs = []
    monkeypatch.setattr(settlement, "default_state_path", lambda: path)
    monkeypatch.setattr(settlement.roles, "load_registry", lambda: object())

    def available(*args, **kwargs):
        kwargs["endpoint_events"].append({"provider": "commandcode"})
        return True, None

    monkeypatch.setattr(settlement, "_role_availability", available)
    monkeypatch.setattr(settlement, "_append_log", lambda record, state=None: logs.append(record))
    assert (
        settlement._reactive_failover(
            {},
            {"decision_id": "decision-1", "_runtime_state": state},
            "review",
            "review",
            {"provider": "opencode", "unavailable_until": 9999999999.0},
        )
        is not None
    )
    assert (
        settlement._reactive_failover(
            {},
            {"decision_id": "decision-1", "_runtime_state": state},
            "review",
            "review",
            {"provider": "commandcode"},
        )
        is None
    )
    assert len([record for record in logs if record["event"] == "reactive_failover"]) == 1
    assert load_state(path).get_execution("decision-1").reactive_respawns["review"]["count"] == 1
