from __future__ import annotations

import json
import pytest

from grokbuild import hook


def test_finish_pre_tool_denies_when_telemetry_raises(monkeypatch, capsys):
    monkeypatch.setattr(
        hook,
        "_append_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")),
    )
    with pytest.raises(SystemExit) as exc:
        hook._finish_pre_tool("deny", "zero_write", detail="blocked")
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


def test_finish_pre_tool_allows_when_telemetry_raises(monkeypatch, capsys):
    monkeypatch.setattr(
        hook,
        "_append_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")),
    )
    hook._finish_pre_tool("allow", "read_only")
    assert json.loads(capsys.readouterr().out)["decision"] == "allow"
