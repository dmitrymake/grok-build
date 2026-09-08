from __future__ import annotations

from grokbuild import hook


def test_writable_spawn_forces_all_without_binding():
    data = {"toolInput": {"subagent_type": "implement-hard"}}
    assert hook._spawn_updated_input(data, None, force_capability=True)["capability_mode"] == "all"


def test_writable_spawn_replaces_read_only_capability_in_nested_input():
    data = {
        "toolInput": {
            "tool_input": {"subagent_type": "implement-hard", "capability_mode": "read-only"}
        }
    }
    updated = hook._spawn_updated_input(data, None, force_capability=True)
    assert updated["tool_input"]["capability_mode"] == "all"


def test_read_only_spawn_is_not_rewritten_without_binding():
    assert hook._spawn_updated_input({"toolInput": {"subagent_type": "explore"}}, None) is None
