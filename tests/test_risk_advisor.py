from __future__ import annotations

import json
from pathlib import Path

from grokbuild.compose import compose_execution
from grokbuild.policy import Profile, load_profiles
from grokbuild.roles import RECON_ROLES, load_registry
from grokbuild.router import route_prompt

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.toml"
PROFILES = ROOT / "grokbuild" / "profiles.json"


def _prompt(words: int) -> str:
    return "route=implement: " + " ".join(["update"] * words)


def _recon_members(decision):
    stage = next(stage for stage in decision.execution if stage.stage_id == "recon")
    return stage.members


def test_default_profile_has_no_advisor_field():
    raw = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]["default"]
    profile = load_profiles()["default"]
    assert "recon_advisor_role" not in raw
    assert "recon_advisor_role" not in profile.to_dict()
    assert profile.recon_advisor_role is None
    assert load_profiles()["advisor"].recon_advisor_role == "explore-risk"


def test_default_composition_excludes_advisor_at_medium_and_high():
    for complexity, expected_width in (("medium", 3), ("high", 5)):
        stages = compose_execution(
            "implement",
            complexity,
            "medium",
            "implement-standard",
            None,
            (),
            review_hard_ok=True,
            profile=Profile("default"),
            explore_risk_ok=True,
        )
        members = next(stage.members for stage in stages if stage.stage_id == "recon")
        assert len(members) == expected_width
        assert "explore-risk" not in {member.role for member in members}


def test_default_static_composition_is_credential_invariant(monkeypatch):
    registry = load_registry(CONFIG)
    profiles = load_profiles()
    for words in (35, 85):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        without_credential = route_prompt(
            _prompt(words),
            registry=registry,
            profiles=profiles,
            profile_name="default",
            mode="static",
        )
        monkeypatch.setenv("MINIMAX_API_KEY", "test-only")
        with_credential = route_prompt(
            _prompt(words),
            registry=registry,
            profiles=profiles,
            profile_name="default",
            mode="static",
        )
        assert [stage.to_dict() for stage in without_credential.execution] == [
            stage.to_dict() for stage in with_credential.execution
        ]
        assert "explore-risk" not in {member.role for member in _recon_members(with_credential)}


def test_advisor_profile_environment_selects_and_appends_available_lens(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-only")
    monkeypatch.setenv("GROK_ROUTE_PROFILE", "advisor")
    decision = route_prompt(
        _prompt(35),
        registry=load_registry(CONFIG),
        profiles=load_profiles(),
        mode="static",
    )
    members = _recon_members(decision)
    assert [member.member_id for member in members] == ["0", "1", "2", "3"]
    assert members[-1].role == "explore-risk"
    assert sum(member.role == "explore-risk" for member in members) == 1
    assert all(member.role in RECON_ROLES for member in members)
    assert not any("recon advisor unavailable" in warning for warning in decision.warnings)


def test_live_equivalent_binding_uses_configured_credential_env(monkeypatch, tmp_path):
    config = CONFIG.read_text(encoding="utf-8").replace(
        '[subagents.roles.explore-risk]\nmodel = "minimax-m3"\nreasoning_effort = "high"',
        '[subagents.roles.explore-risk]\nmodel = "mimo-2.5"\nreasoning_effort = "high"',
    )
    path = tmp_path / "config.toml"
    path.write_text(config, encoding="utf-8")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    unavailable = route_prompt(
        _prompt(35),
        registry=load_registry(path),
        profiles=load_profiles(),
        profile_name="advisor",
        mode="static",
    )
    assert "explore-risk" not in {member.role for member in _recon_members(unavailable)}
    monkeypatch.setenv("MINIMAX_API_KEY", "test-only")
    available = route_prompt(
        _prompt(35),
        registry=load_registry(path),
        profiles=load_profiles(),
        profile_name="advisor",
        mode="static",
    )
    assert _recon_members(available)[-1].role == "explore-risk"


def test_advisor_profile_degrades_immediately_without_credential(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    decision = route_prompt(
        _prompt(35),
        registry=load_registry(CONFIG),
        profiles=load_profiles(),
        profile_name="advisor",
        mode="static",
    )
    members = _recon_members(decision)
    assert len(members) == 3
    assert "explore-risk" not in {member.role for member in members}
    assert any("recon advisor unavailable" in warning for warning in decision.warnings)
    assert ("availability", "explore_risk_unavailable") in decision.stage_trace
