from __future__ import annotations

import os

from grokbuild import hook, pipeline
from grokbuild.state import default_state_path, load_state


def test_hook_prompt_duplicate_is_idempotent_and_new_prompt_advances(tmp_path, monkeypatch):
    """Pin fingerprint wiring through UserPromptSubmit, not allocate_turn directly."""
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    os.makedirs(os.environ["GROK_HOME"], exist_ok=True)
    os.makedirs(os.environ["XDG_STATE_HOME"], exist_ok=True)
    os.makedirs(os.environ["XDG_CACHE_HOME"], exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = pipeline.apply_config_verifiers(hook.load_intents())
    first = {
        "sessionId": "fingerprint-session",
        "prompt": "Add a unit test for an existing function",
        "workspaceRoot": str(workspace),
    }

    hook.handle_prompt(first, spec)
    hook.handle_prompt(dict(first), spec)

    state = load_state(default_state_path())
    tracks = [
        track for track in state.executions.values() if track.session_id == "fingerprint-session"
    ]
    assert len(tracks) == 1
    track = tracks[0]
    assert state.turns["fingerprint-session"] == 1
    terminal = set(track.requested) | set(track.completed) | set(track.failed)
    for stage in track.stages:
        if not stage.required or stage.kind == "verify":
            continue
        keys = (
            [f"{stage.stage_id}/{member.member_id}" for member in stage.members]
            if stage.kind == "parallel_spawn"
            else [stage.role]
        )
        assert terminal <= set(keys)
    assert (
        state.latest_session_debt(
            "fingerprint-session",
            pipeline.load_profiles()["default"].debt_window_seconds,
            excluded_decision_id=track.decision_id,
        )
        is None
    )

    second = dict(first, prompt="Fix the build; tests are red")
    hook.handle_prompt(second, spec)
    state = load_state(default_state_path())
    tracks = [
        track for track in state.executions.values() if track.session_id == "fingerprint-session"
    ]
    assert len(tracks) == 2
    assert state.turns["fingerprint-session"] == 2
    assert {track.turn_id for track in tracks} == {1, 2}


def test_prompt_fingerprint_includes_text_after_200_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("GROK_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        os.makedirs(os.environ[name], exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prefix = "x" * 250
    first = {
        "sessionId": "long-fingerprint",
        "prompt": prefix + "A",
        "workspaceRoot": str(workspace),
    }
    second = dict(first, prompt=prefix + "B")
    spec = pipeline.apply_config_verifiers(hook.load_intents())
    hook.handle_prompt(first, spec)
    hook.handle_prompt(second, spec)
    state = load_state(default_state_path())
    tracks = [t for t in state.executions.values() if t.session_id == "long-fingerprint"]
    assert len(tracks) == 2
    assert {t.turn_id for t in tracks} == {1, 2}
