from __future__ import annotations

import json

import pytest

from grokbuild import hook
from grokbuild.safe_alternatives import safe_alternative


def test_hint_absent_without_mapping() -> None:
    assert safe_alternative("unmapped_denial") is None


def test_zero_write_hint_reaches_output_and_telemetry(monkeypatch, capsys) -> None:
    records: list[dict[str, object]] = []
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))

    with pytest.raises(SystemExit) as exc:
        hook._finish_pre_tool("deny", "zero_write", detail="blocked")

    assert exc.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["safe_alternative"]["kind"] == "delegate_writable_child"
    assert records[0]["safe_alternative"] == output["safe_alternative"]
    assert records[0]["reason_code"] == "zero_write"


@pytest.mark.parametrize(
    ("command", "operation_class", "kind"),
    [
        ("rm -rf /var/lib/sample/old-part", "deletion", "rename"),
        (
            "sudo sed -i 's/max_threads=8/max_threads=4/' /etc/sample/server.conf",
            "server_configuration",
            "query_setting",
        ),
        ("kubectl apply -f recovery-state.yaml", "manual_application", "recovery_workflow"),
    ],
)
def test_blocked_terminal_operation_reaches_reviewed_safe_alternative(
    command: str,
    operation_class: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records: list[dict[str, object]] = []
    route = {
        "mode": "dynamic",
        "decision_id": "decision-1",
        "turn_id": 1,
        "intent": "implement",
        "role": "implement-hard",
        "required_model": "gpt-5.6-sol",
        "required_effort": "xhigh",
        "would_deny_edits": True,
        "would_block_stop": True,
        "execution_valid": True,
        "role_spawnable": True,
    }
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_gate_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(hook, "_ensure_track", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))

    with pytest.raises(SystemExit) as exc:
        hook.handle_pre_tool(
            {
                "sessionId": "session-1",
                "toolName": "run_terminal_command",
                "toolInput": {"command": command},
                "cwd": "/tmp",
            },
            {},
        )

    assert exc.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["safe_alternative"]["kind"] == kind
    assert records[-1]["reason_code"] in {"zero_write", "unknown_write_tool"}
    assert records[-1]["operation_class"] == operation_class
    assert records[-1]["safe_alternative"] == output["safe_alternative"]


def test_malformed_mapping_is_ignored() -> None:
    assert safe_alternative("bad", mapping={"bad": "not an object"}) is None
    assert (
        safe_alternative(
            "bad",
            mapping={
                "bad": {
                    "kind": "rename",
                    "description": "Preserve data.",
                    "reversibility": "sometimes",
                    "operator_context": "Check the target.",
                }
            },
        )
        is None
    )


def test_unknown_reversibility_is_preserved() -> None:
    hint = safe_alternative("manual_application")
    assert hint is not None
    assert hint["kind"] == "recovery_workflow"
    assert hint["reversibility"] == "unknown"


def test_hint_is_recursively_redacted_before_output_and_persistence(monkeypatch, capsys) -> None:
    injected = safe_alternative(
        "injected",
        mapping={
            "injected": {
                "kind": "rename",
                "description": "Use token=supersecret through the nested path.",
                "reversibility": "reversible",
                "operator_context": "Preserve data.",
            }
        },
    )
    assert injected is not None
    monkeypatch.setattr(hook, "safe_alternative", lambda _kind, **_kwargs: injected)
    records: list[dict[str, object]] = []
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))

    with pytest.raises(SystemExit):
        hook._finish_pre_tool("deny", "injected", detail="token=othersecret")

    raw_output = capsys.readouterr().out
    assert "supersecret" not in raw_output
    assert "othersecret" not in raw_output
    assert "supersecret" not in json.dumps(records)
    assert (
        records[0]["safe_alternative"]["description"] == "Use [redacted] through the nested path."
    )
