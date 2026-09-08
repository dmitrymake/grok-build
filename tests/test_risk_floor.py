from __future__ import annotations

import json
from pathlib import Path

import pytest

from grokbuild.classify import load_intents
from grokbuild.evidence import evidence_path
from grokbuild.features import extract_features
from grokbuild.pipeline import Pipeline, select_implement_role
from grokbuild.roles import load_registry
from grokbuild.state import RuntimeState

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = (ROOT / "config" / "config.toml", ROOT / "config" / "config.example.toml")


@pytest.mark.parametrize("config_path", CONFIGS)
def test_overflow_medium_risk_ceiling_parsed_from_repo_surfaces(config_path: Path) -> None:
    assert load_registry(config_path).get("implement-overflow").risk_ceiling == "medium"


def _primary_unavailable() -> RuntimeState:
    state = RuntimeState()
    for role in ("implement-hard", "implement-strong", "implement-standard", "implement-cheap"):
        state.set_available(role, False, "model-upstream unavailable")
    return state


def test_high_risk_never_selects_medium_ceiling_overflow() -> None:
    registry = load_registry(CONFIGS[0])
    features = extract_features("credential migration", load_intents())
    role, reasons, degraded = select_implement_role(
        "high", "high", features, _primary_unavailable(), registry, None
    )
    assert role is None
    assert degraded is True
    assert "skipped implement-overflow (risk ceiling exceeded)" in reasons


def test_medium_risk_medium_ceiling_overflow_remains_permitted() -> None:
    registry = load_registry(CONFIGS[0])
    features = extract_features("repository migration", load_intents())
    role, _reasons, degraded = select_implement_role(
        "medium", "medium", features, _primary_unavailable(), registry, None
    )
    assert role == "implement-overflow"
    assert degraded is True


def test_high_risk_exhaustion_blocks_both_gates_without_spawnable_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state = _primary_unavailable()
    decision = Pipeline(
        registry=load_registry(CONFIGS[0]),
        mode="dynamic",
        state=state,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "route.jsonl",
    ).run(
        "route=implement: migrate credential storage across the repository",
        session_id="risk-session",
        persist=True,
    )

    assert decision.risk == "high"
    assert decision.role_spawnable is False
    assert decision.would_deny_edits is True
    assert decision.would_block_stop is True
    assert decision.write_policy == "deny"
    assert decision.allowed is False
    assert any(
        "BLOCKED: no capable+available model of class high; quota/human needed." in warning
        for warning in decision.warnings
    )
    records = [
        json.loads(line) for line in evidence_path().read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["subject"].get("path") == "implement-selection" for record in records)
    assert all(record["decision_id"] == decision.decision_id for record in records)


@pytest.mark.parametrize("ceiling", ["low", "medium", "high"])
def test_valid_risk_ceilings_pass_registry_validation(tmp_path: Path, ceiling: str) -> None:
    config = (
        CONFIGS[0]
        .read_text(encoding="utf-8")
        .replace('risk_ceiling = "medium"', f'risk_ceiling = "{ceiling}"')
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    registry = load_registry(config_path)
    assert registry.get("implement-overflow").risk_ceiling == ceiling
    assert not [
        issue
        for issue in registry.validate_roles()
        if issue["role"] == "implement-overflow" and issue["level"] == "error"
    ]


def test_invalid_risk_ceiling_fails_registry_validation(tmp_path: Path) -> None:
    config = (
        CONFIGS[0]
        .read_text(encoding="utf-8")
        .replace('risk_ceiling = "medium"', 'risk_ceiling = "medum"')
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    registry = load_registry(config_path)
    role = registry.get("implement-overflow")
    assert role.risk_ceiling is None
    assert {
        "level": "error",
        "role": "implement-overflow",
        "msg": "risk_ceiling must be one of: low, medium, high",
    } in registry.validate_roles()


def test_absent_risk_ceiling_preserves_legacy_unrestricted_behavior(tmp_path: Path) -> None:
    config = CONFIGS[0].read_text(encoding="utf-8").replace('risk_ceiling = "medium"\n', "")
    config_path = tmp_path / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    registry = load_registry(config_path)
    assert registry.get("implement-overflow").risk_ceiling is None

    features = extract_features("credential migration", load_intents())
    role, _reasons, _degraded = select_implement_role(
        "high", "high", features, _primary_unavailable(), registry, None
    )
    assert role == "implement-overflow"
