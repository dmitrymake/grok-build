from __future__ import annotations

import json

from _harness import _Stdin
from grokbuild import hook


def test_corrupt_pretooluse_stdin_uses_event_environment(monkeypatch, capsys):
    monkeypatch.setenv("GROK_HOOK_EVENT", "PreToolUse")
    monkeypatch.setattr("sys.stdin", _Stdin("not-json"))
    monkeypatch.setattr(hook, "_STDIN_UNPARSEABLE", False)
    records = []
    monkeypatch.setattr(hook, "_append_log", lambda record, **_: records.append(record))

    try:
        hook.main()
    except SystemExit as exc:
        assert exc.code == 2
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["decision"] == "deny"
    assert records[-1]["reason_code"] == "stdin_unparseable"


def test_state_set_zero_failures_preserves_availability(tmp_path, monkeypatch):
    from grokbuild import cli
    from grokbuild.state import RuntimeState, save_state

    state_path = tmp_path / "state.json"
    state = RuntimeState(source_path=state_path)
    status = state.status_for("review-hard")
    status.available = False
    status.reason = "operator hold"
    status.consecutive_failures = 4
    save_state(state, state_path)
    monkeypatch.setattr(cli, "default_state_path", lambda: state_path)

    assert cli.main(["state", "set", "--role", "review-hard", "--failures", "0"]) == 0
    updated = RuntimeState.from_dict(json.loads(state_path.read_text()))
    status = updated.status_for("review-hard")
    assert status.consecutive_failures == 0
    assert status.available is False
    assert status.reason == "operator hold"


def test_state_set_positive_failures_marks_unavailable(tmp_path, monkeypatch):
    from grokbuild import cli
    from grokbuild.state import RuntimeState, save_state

    state_path = tmp_path / "state.json"
    save_state(RuntimeState(source_path=state_path), state_path)
    monkeypatch.setattr(cli, "default_state_path", lambda: state_path)

    assert cli.main(["state", "set", "--role", "review-hard", "--failures", "3"]) == 0
    updated = RuntimeState.from_dict(json.loads(state_path.read_text()))
    status = updated.status_for("review-hard")
    assert status.consecutive_failures == 3
    assert status.available is False
