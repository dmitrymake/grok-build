"""Regressions for the 2026-09-04 deepseek 429 incident (audit findings B1-B3).

The failed child was an ad-hoc consilium member spawned after the pipeline had
finished (``gate_inactive``), and its 429 arrived inside a ``wait_all``
retrieval section rather than a spawn result. Neither path used to reach the
quota circuit, and the ungated spawn never received an endpoint binding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grokbuild import hook, payloads, settlement
from grokbuild.state import RuntimeState, load_state

ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = ROOT / "config" / "config.toml"
PARENT = "01a068c4-21ac-7110-bc01-1624706aa973"
ANALYST = "01a06ac8-b184-7b80-932c-7456d9a1a70b"
CHALLENGER = "01a06ac8-b185-7052-b8ec-f80439334a56"
MODEL = "qwen3.8-max"

INCIDENT_TEXT = f"""=== Multi-wait (wait_all) ===
--- Task {ANALYST} [running] ---
Command: [subagent:plan-hard] Consilium analyst: FEAL attack
Duration: 613.91s
Subagent is still running.
Type: plan-hard
Description: Consilium analyst: FEAL attack
Elapsed: 613.9s
Progress: turn 1, 40 tool calls, 99K/1000K tokens (9% context)
Tools used: list_dir, grep, read_file
Errors: 3

Waited the requested 600s; the subagent is still running. You will be notified automatically when the subagent completes.
--- Task {CHALLENGER} [failed] ---
Command: [subagent:review-hard] Consilium challenger: FEAL attack
Duration: 126.07s
Exit Code: 1
Session error: Rate limited: "API error (status 429 Too Many Requests): GoUsageLimitError: Monthly usage limit reached. Resets in 1 day. To continue using this model now, enable usage from your available balance"

1/2 tasks completed (wait_all)
"""

CHILD_FAILURE_BODY = """Command: [subagent:review-hard] Consilium challenger: FEAL attack
Duration: 126.07s
Exit Code: 1
Session error: Rate limited: "API error (status 429 Too Many Requests): GoUsageLimitError: Monthly usage limit reached. Resets in 1 day."
"""


def _explicit_endpoint_config(tmp_path: Path) -> Path:
    """Live-shaped config: review-hard and plan-hard run on a two-endpoint model."""
    config = REPO_CONFIG.read_text(encoding="utf-8")
    review_old = '''[subagents.roles.review-hard]
description = "Internal read-only review for medium/high-complexity work."
model = "glm-5.3"'''
    plan_old = '''[subagents.roles.plan-hard]
description = "Hard planning, read-only."
model = "gpt-6-astra"'''
    assert review_old in config and plan_old in config
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir(exist_ok=True)
    (grok_home / "config.toml").write_text(
        config.replace(review_old, review_old.replace("glm-5.3", MODEL), 1).replace(
            plan_old, plan_old.replace("gpt-6-astra", MODEL), 1
        ),
        encoding="utf-8",
    )
    return grok_home


def _plant(grok_home: Path, session_id: str, *, model: str, kind: str | None = None) -> None:
    folder = grok_home / "sessions" / "ws" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    summary = {"info": {"id": session_id}, "current_model_id": model}
    if kind:
        summary["session_kind"] = kind
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (folder / "chat_history.jsonl").write_text("", encoding="utf-8")


@pytest.fixture
def incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    grok_home = _explicit_endpoint_config(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _plant(grok_home, PARENT, model="glm-5.3-flash")
    _plant(grok_home, ANALYST, model=MODEL, kind="subagent")
    _plant(grok_home, CHALLENGER, model=MODEL, kind="subagent")
    state = RuntimeState()
    route = {
        "decision_id": "5cc8ff814c6f21f8628c",
        "session_id": PARENT,
        "mode": "dynamic",
        "intent": "implement",
        "source": "transcript",
        "_runtime_state": state,
    }
    records: list[dict] = []
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))
    monkeypatch.setattr(hook, "_load_execution", lambda *_args: None)
    return grok_home, route, records


def _retrieval_payload(text: str = INCIDENT_TEXT) -> dict:
    return {
        "sessionId": PARENT,
        "toolName": "get_command_or_subagent_output",
        "toolInput": {"task_ids": [ANALYST, CHALLENGER], "wait_all": True},
        "toolResult": text,
    }


# --- B1: the retrieval path opens the provider circuit ----------------------


def test_incident_retrieval_opens_the_circuit_without_a_binding(incident) -> None:
    """The exact incident shape: ungated consilium, 429 inside a wait_all section."""
    _grok_home, route, records = incident
    hook.handle_post_tool(_retrieval_payload(), {})

    state = load_state(hook.default_state_path())
    circuit = state.provider_availability["opencode"]
    assert circuit.reason == "quota/429" and not state.is_provider_available("opencode")
    assert route["_runtime_state"].is_provider_available("opencode") is False
    assert state.provider_availability.get("commandcode") is None
    record = records[-1]
    assert record["event"] == "post_tool_use"
    assert record["retrieval"]["statuses"][CHALLENGER] == "failure"
    assert record["quota_circuit"]["provider"] == "opencode"
    assert record["quota_circuit"]["task_id"] == CHALLENGER
    assert record["quota_circuit"]["role"] == "review-hard"
    assert record["quota_circuit"]["path"] == "retrieval"


def test_retrieval_circuit_follows_the_child_model_binding(incident) -> None:
    """The child's own session says which endpoint served it."""
    grok_home, _route, records = incident
    _plant(grok_home, CHALLENGER, model=f"{MODEL}@commandcode", kind="subagent")
    hook.handle_post_tool(_retrieval_payload(), {})
    state = load_state(hook.default_state_path())
    assert not state.is_provider_available("commandcode")
    assert state.provider_availability.get("opencode") is None
    assert records[-1]["quota_circuit"]["provider"] == "commandcode"


def test_retrieval_failure_without_quota_leaves_the_circuit_closed(incident) -> None:
    _grok_home, _route, records = incident
    text = INCIDENT_TEXT.replace(
        'Session error: Rate limited: "API error (status 429 Too Many Requests): '
        "GoUsageLimitError: Monthly usage limit reached. Resets in 1 day. "
        'To continue using this model now, enable usage from your available balance"',
        "Session error: context window exhausted",
    )
    assert "429" not in text
    hook.handle_post_tool(_retrieval_payload(text), {})
    assert load_state(hook.default_state_path()).provider_availability == {}
    assert "quota_circuit" not in records[-1]


def test_retrieval_circuit_uses_the_bound_stage_role(incident, monkeypatch) -> None:
    """With a binding the tracked stage decides the role, and the stage still fails."""
    from grokbuild.decision import ExecutionStage
    from grokbuild.state import ExecutionTrack
    from grokbuild.transactions import bind_linear_stage_task_tx, ensure_execution_tx

    _grok_home, route, records = incident
    decision_id = route["decision_id"]
    stage = ExecutionStage("plan-hard", True, "plan the attack")
    ensure_execution_tx(hook.default_state_path(), decision_id, PARENT, 1, [stage.to_dict()])
    state = load_state(hook.default_state_path())
    track = state.get_execution(decision_id)
    assert isinstance(track, ExecutionTrack)
    track.requested.append("plan-hard")
    state.executions[decision_id] = track
    hook.default_state_path().write_text(json.dumps(state.to_dict()), encoding="utf-8")
    assert bind_linear_stage_task_tx(
        hook.default_state_path(), decision_id, "plan-hard", CHALLENGER
    )
    text = INCIDENT_TEXT.replace("[subagent:review-hard]", "[subagent:unknown-type]")

    hook.handle_post_tool(_retrieval_payload(text), {})

    after = load_state(hook.default_state_path())
    assert not after.is_provider_available("opencode")
    assert records[-1]["quota_circuit"]["role"] == "plan-hard"
    assert records[-1]["retrieval"]["outcomes"][CHALLENGER] == "recorded"
    assert "plan-hard" in after.get_execution(decision_id).failed


def test_blocked_child_sweep_reports_quota(incident, monkeypatch) -> None:
    """A BLOCKED child citing a 429 also holds its provider at Stop time."""
    _grok_home, route, _records = incident
    monkeypatch.setattr(settlement, "pending_child_bindings", lambda _state, _session: [CHALLENGER])
    monkeypatch.setattr(settlement, "_pending_child_role", lambda _session, _task: "review-hard")
    monkeypatch.setattr(
        settlement,
        "_last_child_assistant_text",
        lambda _task, history_path=None: "BLOCKED: provider returned 429 quota exhausted",
    )
    settlement._sweep_pending_children(PARENT, route)
    assert not load_state(hook.default_state_path()).is_provider_available("opencode")


# --- B2: ungated spawns are still endpoint-bound ----------------------------


def _primary_first_session() -> str:
    """A session id whose rotate walk visits the declared primary endpoint first."""
    import hashlib

    for suffix in range(64):
        candidate = f"01a068c4-21ac-7110-bc01-1624706aa{suffix:03x}"
        if int(hashlib.sha256(candidate.encode()).hexdigest()[:16], 16) % 2 == 0:
            return candidate
    raise AssertionError("no primary-first session id found")


def _pre_tool_spawn(
    monkeypatch, capsys, route, role: str, mode: str = "dynamic"
) -> tuple[dict, dict]:
    route = dict(route, mode=mode)
    records: list[dict] = []
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_gate_active", lambda *args, **kwargs: False)
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))
    hook.handle_pre_tool(
        {
            "sessionId": _primary_first_session(),
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": role,
                "prompt": "Challenge the plan.",
                "background": True,
            },
        },
        {},
    )
    return json.loads(capsys.readouterr().out), records[-1]


def test_ungated_spawn_is_rebound_away_from_a_held_provider(incident, monkeypatch, capsys) -> None:
    _grok_home, route, _records = incident
    hook.handle_post_tool(_retrieval_payload(), {})  # the incident opened the circuit
    route["_runtime_state"] = load_state(hook.default_state_path())

    output, record = _pre_tool_spawn(monkeypatch, capsys, route, "review-hard")

    assert output["decision"] == "allow"
    assert record["reason_code"] == "gate_inactive"
    assert output["hookSpecificOutput"]["updatedInput"]["model"] == f"{MODEL}@commandcode"
    assert output["hookSpecificOutput"]["updatedInput"]["background"] is True
    resolution = record["endpoint_resolution"]
    assert resolution["selected"] == f"{MODEL}@commandcode"
    assert resolution["provider"] == "commandcode"
    assert any("quota/429" in skip["reason"] for skip in resolution["skipped"])


def test_ungated_spawn_of_a_single_endpoint_role_is_unchanged(
    incident, monkeypatch, capsys
) -> None:
    _grok_home, route, _records = incident
    output, record = _pre_tool_spawn(monkeypatch, capsys, route, "implement-standard")
    assert output == {"decision": "allow"}
    assert record["reason_code"] == "gate_inactive"
    assert record.get("endpoint_resolution") is None


def test_static_mode_never_rebinds(incident, monkeypatch, capsys) -> None:
    _grok_home, route, _records = incident
    route["_runtime_state"].set_provider_unavailable("opencode", 4102444800.0, "quota/429")
    output, record = _pre_tool_spawn(monkeypatch, capsys, route, "review-hard", mode="static")
    assert output == {"decision": "allow"}
    assert record.get("endpoint_resolution") is None


# --- B3: spawn results are classified from status fields and terminal lines --


@pytest.mark.parametrize(
    "result",
    [
        {"status": "failed", "exit_code": 1, "output": "Done: everything looks great"},
        {"status": "completed", "exit_code": 1, "output": "Done: summary"},
        {"status": "cancelled", "output": "Done: summary"},
        {"status": 429, "error": "quota exhausted"},
        {"error": "quota exhausted"},
        CHILD_FAILURE_BODY,
        {"content": CHILD_FAILURE_BODY},
        "Review complete.\nExit Code: 2",
        "Review complete.\nSession error: Rate limited",
    ],
)
def test_structured_and_terminal_line_failures_classify_as_failure(result) -> None:
    assert payloads.spawn_result_status({"toolResult": result}) == "failure"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "completed"}, "incomplete"),
        ({"status": "running", "output": "Done: summary"}, "incomplete"),
        ({"status": "completed", "exit_code": 0, "output": "Done: summary"}, "success"),
        ({"exit_code": 0, "output": "Done: summary"}, "success"),
        ("Review complete.\nExit Code: 0\nall good", "success"),
        ("Review complete: the session error handling is fine.", "success"),
    ],
)
def test_structured_fields_never_manufacture_a_verdict(result, expected) -> None:
    assert payloads.spawn_result_status({"toolResult": result}) == expected


def test_foreground_429_result_fails_the_stage_and_opens_the_circuit(incident) -> None:
    _grok_home, _route, records = incident
    hook.handle_post_tool(
        {
            "sessionId": PARENT,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "prompt": "Challenge the plan."},
            "toolResult": CHILD_FAILURE_BODY,
        },
        {},
    )
    assert not load_state(hook.default_state_path()).is_provider_available("opencode")
    assert records[-1]["quota_circuit"]["provider"] == "opencode"


def test_child_diagnostic_about_rate_limit_validation_does_not_open_circuit(incident) -> None:
    _grok_home, route, records = incident
    hook.handle_post_tool(
        {
            "sessionId": PARENT,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "prompt": "Review quota handling."},
            "toolResult": {
                "status": "failed",
                "error": "BLOCKED: the rate-limit validation in the reviewed implementation is incorrect",
            },
        },
        {},
    )
    assert load_state(hook.default_state_path()).provider_availability == {}
    assert route["_runtime_state"].provider_availability == {}
    assert "quota_circuit" not in records[-1]


def test_retrieval_sections_and_roles_are_parsed() -> None:
    sections = payloads.retrieval_task_sections(INCIDENT_TEXT)
    assert set(sections) == {ANALYST, CHALLENGER}
    assert sections[CHALLENGER].startswith(f"--- Task {CHALLENGER} [failed] ---")
    assert "429" in sections[CHALLENGER] and "429" not in sections[ANALYST]
    assert payloads.retrieval_section_role(sections[CHALLENGER]) == "review-hard"
    assert payloads.retrieval_section_role(sections[ANALYST]) == "plan-hard"
    assert payloads.retrieval_section_role("Command: pytest -q\nExit Code: 1") == ""
    assert payloads.retrieval_task_sections(None) == {}


# --- B4: the circuit provider follows the failed role's own model ------------


def test_circuit_attribution_follows_the_failed_roles_model(incident) -> None:
    """Two explicit-endpoint roles in one decision resolve independently."""
    from grokbuild.endpoint_resolution import make_endpoint_resolution, persist_endpoint_resolution

    _grok_home, route, records = incident
    decision_id = route["decision_id"]
    persist_endpoint_resolution(
        make_endpoint_resolution(
            decision_id=decision_id,
            session_id=PARENT,
            event={"model": MODEL, "selected": f"{MODEL}@opencode", "provider": "opencode"},
        )
    )
    persist_endpoint_resolution(
        make_endpoint_resolution(
            decision_id=decision_id,
            session_id=PARENT,
            event={
                "model": "deepseek-v4-pro",
                "selected": "deepseek-v4-pro@commandcode",
                "provider": "commandcode",
            },
        )
    )
    hook.handle_post_tool_failure(
        {
            "sessionId": PARENT,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "prompt": "Challenge the plan."},
            "toolResult": {"status": 429, "error": "quota exhausted"},
        },
        {},
    )
    state = load_state(hook.default_state_path())
    assert not state.is_provider_available("opencode")
    assert state.provider_availability.get("commandcode") is None
    assert records[-1]["quota_circuit"]["provider"] == "opencode"


# --- P0.4: reactive failover reopens only tracked stages ---------------------


def _bind_plan_stage(route: dict) -> None:
    from grokbuild.decision import ExecutionStage
    from grokbuild.transactions import bind_linear_stage_task_tx, ensure_execution_tx

    decision_id = route["decision_id"]
    stage = ExecutionStage("plan-hard", True, "plan the attack")
    ensure_execution_tx(hook.default_state_path(), decision_id, PARENT, 1, [stage.to_dict()])
    state = load_state(hook.default_state_path())
    state.get_execution(decision_id).requested.append("plan-hard")
    hook.default_state_path().write_text(json.dumps(state.to_dict()), encoding="utf-8")
    assert bind_linear_stage_task_tx(
        hook.default_state_path(), decision_id, "plan-hard", CHALLENGER
    )
    route["_runtime_state"] = load_state(hook.default_state_path())


def test_retrieval_429_on_a_tracked_stage_marks_the_reserve_retry(incident) -> None:
    from grokbuild import gate

    _grok_home, route, records = incident
    _bind_plan_stage(route)

    hook.handle_post_tool(_retrieval_payload(), {})

    state = load_state(hook.default_state_path())
    track = state.get_execution(route["decision_id"])
    assert "plan-hard" in track.failed and "plan-hard" not in track.completed
    marker = track.reactive_respawns["plan-hard"]
    assert marker["from_provider"] == "opencode" and marker["to_provider"] == "commandcode"
    assert marker["count"] == 1
    events = [record for record in records if record.get("event") == "reactive_failover"]
    assert len(events) == 1 and events[0]["role"] == "plan-hard"
    assert records[-1]["quota_circuit"]["reactive_failover"]["to_provider"] == "commandcode"
    recipe = gate._stage_recipe({"intent": "implement"}, "plan-hard", track)
    assert "primary opencode quota-held" in recipe and "reserve commandcode" in recipe


def test_retrieval_429_without_a_tracked_stage_holds_without_a_retry_marker(incident) -> None:
    """The incident shape: nothing to reopen, so only the hold and the rebind act."""
    _grok_home, route, records = incident
    hook.handle_post_tool(_retrieval_payload(), {})
    state = load_state(hook.default_state_path())
    assert not state.is_provider_available("opencode")
    assert all(not track.reactive_respawns for track in state.executions.values())
    assert not any(record.get("event") == "reactive_failover" for record in records)
    assert "reactive_failover" not in records[-1]["quota_circuit"]


def test_reserve_429_on_the_same_stage_does_not_retry_again(incident) -> None:
    grok_home, route, records = incident
    _bind_plan_stage(route)
    hook.handle_post_tool(_retrieval_payload(), {})
    # The reserve now fails the respawned stage: bind the new child and fail it too.
    from grokbuild.transactions import bind_linear_stage_task_tx

    reserve_child = "01a06ac8-b186-7052-b8ec-f80439334a57"
    _plant(grok_home, reserve_child, model=f"{MODEL}@commandcode", kind="subagent")
    state = load_state(hook.default_state_path())
    state.get_execution(route["decision_id"]).requested.append("plan-hard")
    hook.default_state_path().write_text(json.dumps(state.to_dict()), encoding="utf-8")
    assert bind_linear_stage_task_tx(
        hook.default_state_path(), route["decision_id"], "plan-hard", reserve_child
    )
    route["_runtime_state"] = load_state(hook.default_state_path())
    text = INCIDENT_TEXT.replace(CHALLENGER, reserve_child)
    hook.handle_post_tool(
        {
            "sessionId": PARENT,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [ANALYST, reserve_child], "wait_all": True},
            "toolResult": text,
        },
        {},
    )
    state = load_state(hook.default_state_path())
    assert not state.is_provider_available("commandcode")
    assert state.get_execution(route["decision_id"]).reactive_respawns["plan-hard"]["count"] == 1
    events = [record for record in records if record.get("event") == "reactive_failover"]
    assert len(events) == 1
