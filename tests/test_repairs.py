from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from grokbuild.cli import _parse_until
from grokbuild.payloads import spawn_background
from grokbuild.persist import state_dir
from grokbuild.policy import Profile, resolve_mode_from_env
from grokbuild.shell_guard import _tool_command


def test_parse_until_requires_timezone() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="include a timezone"):
        _parse_until("2026-09-01")
    assert _parse_until("2026-09-01T00:00:00+03:00")
    assert _parse_until("2026-09-01T00:00:00Z")


def test_state_dir_empty_xdg_is_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert state_dir() == Path("grok-route")
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_dir() == Path.home() / ".local" / "state" / "grok-route"
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/grok-state")
    assert state_dir() == Path("/tmp/grok-state/grok-route")


def test_outer_tool_payload_semantics() -> None:
    data = {"toolInput": {"command": "outer", "tool_input": {"command": "nested"}}}
    assert _tool_command(data) == "outer"
    assert (
        spawn_background({"toolInput": {"background": False, "tool_input": {"background": True}}})
        is False
    )


def test_mode_environment_fallbacks() -> None:
    profile = Profile(name="test", mode="shadow")
    assert resolve_mode_from_env({}, profile) == "shadow"
    assert resolve_mode_from_env({"GROK_ROUTE_SOFT": ""}, profile) == "shadow"
    assert resolve_mode_from_env({"GROK_ROUTE_SOFT": "0"}, profile) == "shadow"
    assert resolve_mode_from_env({"GROK_ROUTE_ENFORCE": "1"}, profile) == "dynamic"
    profile_dynamic = Profile(name="test", mode="dynamic")
    assert resolve_mode_from_env({}, profile_dynamic) == "dynamic"
    assert resolve_mode_from_env({"GROK_ROUTE_SOFT": ""}, profile_dynamic) == "dynamic"
    assert resolve_mode_from_env({"GROK_ROUTE_SOFT": "0"}, profile_dynamic) == "shadow"
