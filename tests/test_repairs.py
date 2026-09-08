from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from grokbuild.cli import _parse_until
from grokbuild.payloads import spawn_background, spawn_result_status
from grokbuild.persist import state_dir
from grokbuild.policy import Profile, resolve_mode_from_env
from grokbuild.shell_guard import _tool_command


def test_parse_until_requires_timezone() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="include a timezone"):
        _parse_until("2026-09-01")
    assert _parse_until("2026-09-01T00:00:00+03:00")
    assert _parse_until("2026-09-01T00:00:00Z")


def test_state_dir_empty_and_relative_xdg_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = Path.home() / ".local" / "state" / "grok-route"
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert state_dir() == fallback
    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    assert state_dir() == fallback
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_dir() == fallback
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/grok-state")
    assert state_dir() == Path("/tmp/grok-state/grok-route")


def test_background_failure_is_terminal_but_ack_is_incomplete() -> None:
    assert (
        spawn_result_status({"background": True, "toolResult": "failed: worker exited"})
        == "failure"
    )
    assert (
        spawn_result_status({"background": True, "toolResult": "Task started in background"})
        == "incomplete"
    )


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
