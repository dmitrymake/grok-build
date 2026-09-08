from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from grokbuild import hook, settlement
import grokbuild.pipeline as pipeline
import grokbuild.availability as availability
from grokbuild.classify import load_intents
from grokbuild.decision import decision_hook_record
from grokbuild.discovery import DiscoveryTarget
from grokbuild.endpoint_resolution import endpoint_resolution_path, make_endpoint_resolution
from grokbuild.features import extract_features
from grokbuild.pipeline import Pipeline, select_implement_role
from grokbuild.roles import (
    ExecutableModelBinding,
    Role,
    RoleRegistry,
    load_provider_catalog,
)
from grokbuild.state import RuntimeState

ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG = ROOT / "config" / "config.toml"
MODEL = "deepseek-v4-pro"


def _registry(role_name: str = "implement-overflow") -> RoleRegistry:
    catalog = load_provider_catalog()
    meta = catalog[MODEL]
    role = Role(
        name=role_name,
        model=MODEL,
        capability_mode="all",
        write=True,
        provider=meta.provider,
        provider_label=meta.provider_label,
        tier=meta.tier,
        risk_ceiling="medium" if role_name == "implement-overflow" else None,
    )
    return RoleRegistry(
        {role_name: role},
        provider_catalog=catalog,
        executable_bindings={
            MODEL: ExecutableModelBinding(
                MODEL, "https://opencode.sample/v1", "OPENCODE_GO_API_KEY"
            ),
            f"{MODEL}@commandcode": ExecutableModelBinding(
                f"{MODEL}@commandcode",
                "https://commandcode.sample/v1",
                "COMMANDCODE_API_KEY",
            ),
        },
    )


def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, providers: dict) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = tmp_path / "grok" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "providers": providers}), encoding="utf-8")


def _review_config(tmp_path: Path) -> Path:
    config = REPO_CONFIG.read_text(encoding="utf-8")
    old = '''[subagents.roles.review-hard]
description = "Internal read-only review for medium/high-complexity work."
model = "glm-5.3"
reasoning_effort = "max"
default_capability_mode = "read-only"
autonomy = "standard"'''
    new = old.replace('model = "glm-5.3"', 'model = "qwen3.8-max"')
    plan_old = '''[subagents.roles.plan-hard]
description = "Hard planning, read-only."
model = "gpt-6-astra"
reasoning_effort = "max"
default_capability_mode = "read-only"
autonomy = "broad"'''
    plan_new = plan_old.replace('model = "gpt-6-astra"', 'model = "qwen3.8-max"')
    assert old in config and plan_old in config
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir(exist_ok=True)
    config_path = grok_home / "config.toml"
    config_path.write_text(
        config.replace(old, new, 1).replace(plan_old, plan_new, 1),
        encoding="utf-8",
    )
    return grok_home


def _configured_registry(tmp_path: Path) -> RoleRegistry:
    config = REPO_CONFIG.read_text(encoding="utf-8")
    old = 'model = "deepseek-v4-pro"\nreasoning_effort = "high"\ntier = "overflow"'
    new = f'model = "{MODEL}"\nreasoning_effort = "high"\ntier = "overflow"'
    assert old in config
    config_path = tmp_path / "config.toml"
    config_path.write_text(config.replace(old, new, 1), encoding="utf-8")
    return pipeline.load_registry(config_path)


@pytest.mark.parametrize(
    ("used_percent", "expected_model"),
    ((100.0, "qwen3.8-max@commandcode"), (50.0, None)),
)
def test_pre_tool_spawn_rebinds_against_fresh_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    used_percent: float,
    expected_model: str | None,
) -> None:
    session_id = "quota-rebind"
    prompt = "Review the quota freshness change and report findings."
    grok_home = _review_config(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    monkeypatch.setenv("GROK_ROUTE_ENFORCE", "1")
    _cache(
        tmp_path,
        monkeypatch,
        {
            "qwen3.8-max@opencode": {
                "status": "ok",
                "fetched_at": time.time(),
                "quota": {"windows": [{"used_percent": used_percent}]},
            }
        },
    )
    monkeypatch.setattr(hook, "last_user_prompt_raw", lambda _session_id: prompt)
    spec = pipeline.apply_config_verifiers(load_intents())

    hook.handle_prompt({"sessionId": session_id, "prompt": prompt, "cwd": str(tmp_path)}, spec)
    hook.handle_pre_tool(
        {
            "sessionId": session_id,
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "review-hard",
                "prompt": "Review the implementation.",
            },
            "cwd": str(tmp_path),
        },
        spec,
    )

    output = json.loads(capsys.readouterr().out)
    if expected_model is None:
        assert "hookSpecificOutput" not in output
    else:
        assert (
            output.get("hookSpecificOutput", {}).get("updatedInput", {}).get("model")
            == expected_model
        ), output
    endpoint_records = [
        json.loads(line)
        for line in endpoint_resolution_path().read_text(encoding="utf-8").splitlines()
    ]
    spawn_record = endpoint_records[-1]
    assert spawn_record["selected"] == (expected_model or "qwen3.8-max@opencode")
    assert ["quota pressured" in item["reason"] for item in spawn_record["skipped"]] == (
        [True] if expected_model else []
    )
    telemetry = [
        json.loads(line)
        for line in (tmp_path / "state-home" / "grok-route" / "route.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert telemetry[-1]["endpoint_resolution"] == spawn_record


def test_quota_failure_uses_persisted_endpoint_provider_and_naive_utc_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grok_home = _review_config(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    # A fixed date would expire and flip the assertion; keep the reset in the future.
    reset_at = datetime.now(tz=UTC).replace(microsecond=0) + timedelta(days=2)
    naive_reset = reset_at.replace(tzinfo=None).isoformat()
    _cache(
        tmp_path,
        monkeypatch,
        {
            "qwen3.8-max@commandcode": {
                "status": "ok",
                "quota": {"windows": [{"used_percent": 100.0, "resets_at": naive_reset}]},
            }
        },
    )
    resolution = make_endpoint_resolution(
        decision_id="persisted",
        session_id="s",
        event={
            "model": "qwen3.8-max",
            "selected": "qwen3.8-max@commandcode",
            "provider": "commandcode",
        },
    )
    from grokbuild.endpoint_resolution import persist_endpoint_resolution

    persist_endpoint_resolution(resolution)
    route = {
        "decision_id": "persisted",
        "mode": "dynamic",
        "intent": "review",
        "source": "hook",
        "_runtime_state": RuntimeState(),
    }
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_load_execution", lambda *_args: None)
    monkeypatch.setattr(hook, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(settlement, "record_stage_tx", lambda *args, **kwargs: None)
    before = time.time()
    hook.handle_post_tool_failure(
        {
            "sessionId": "s",
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "model": "qwen3.8-max@opencode"},
            "toolResult": {"status": 429, "error": "quota exhausted"},
            "cwd": str(tmp_path),
        },
        {},
    )
    state = pipeline.load_state(hook.default_state_path())
    assert state.provider_availability["commandcode"].unavailable_until == pytest.approx(
        reset_at.timestamp()
    )
    assert state.provider_availability.get("opencode") is None
    assert state.provider_availability["commandcode"].unavailable_until > before


def test_quota_failure_opens_provider_circuit_until_cached_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grok_home = _review_config(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    reset_at = time.time() + 3600
    _cache(
        tmp_path,
        monkeypatch,
        {
            "qwen3.8-max@opencode": {
                "status": "ok",
                "quota": {
                    "fetched_at": time.time(),
                    "windows": [
                        {
                            "used_percent": 100.0,
                            "resets_at": datetime.fromtimestamp(reset_at, UTC).isoformat(),
                        }
                    ],
                },
            }
        },
    )
    runtime_state = RuntimeState()
    route = {
        "decision_id": "quota-failure",
        "mode": "dynamic",
        "intent": "review",
        "source": "hook",
        "_runtime_state": runtime_state,
    }
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_load_execution", lambda *_args: None)
    monkeypatch.setattr(hook, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(settlement, "record_stage_tx", lambda *args, **kwargs: None)

    hook.handle_post_tool_failure(
        {
            "sessionId": "quota-failure-session",
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "review-hard",
                "prompt": "Review the implementation.",
            },
            "toolResult": {"status": 429, "error": "quota exhausted"},
            "cwd": str(tmp_path),
        },
        {},
    )

    state = pipeline.load_state(hook.default_state_path())
    circuit = state.provider_availability["opencode"]
    assert circuit.reason == "quota/429"
    assert circuit.unavailable_until == pytest.approx(reset_at)
    registry = pipeline.load_registry(grok_home / "config.toml")
    ok, signal, event = availability._model_endpoint_availability(state, "plan-hard", registry)
    assert ok is True
    assert signal is None
    assert event["selected"] == "qwen3.8-max@commandcode"
    assert "quota/429" in event["skipped"][0]["reason"]


def test_fresh_quota_forgives_provider_hold_but_stale_quota_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "fetched_at": time.time(),
                "quota": {"fetched_at": time.time(), "windows": [{"used_percent": 10.0}]},
            }
        },
    )
    state = RuntimeState(source_path=tmp_path / "state.json")
    state.set_provider_unavailable("opencode", time.time() + 300, "provider held")
    ok, signal, event = availability._model_endpoint_availability(
        state, "implement-overflow", _registry()
    )
    assert ok and signal is None and event["selected"] == f"{MODEL}@opencode"
    assert state.is_provider_available("opencode")
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                "quota": {
                    "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                    "windows": [{"used_percent": 10.0}],
                },
            }
        },
    )
    state.set_provider_unavailable("opencode", time.time() + 300, "provider held")
    availability._model_endpoint_availability(state, "implement-overflow", _registry())
    assert not state.is_provider_available("opencode")


def test_opencode_hold_resolves_same_logical_model_through_commandcode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(tmp_path, monkeypatch, {})
    state = RuntimeState()
    state.set_provider_unavailable("opencode", time.time() + 300, "model-upstream unavailable")
    events: list[dict] = []

    role, reasons, degraded = select_implement_role(
        "low",
        "medium",
        extract_features("implement a repository migration", load_intents()),
        state,
        _registry(),
        None,
        endpoint_events=events,
    )

    assert role == "implement-overflow"
    assert degraded is True
    assert events[-1]["model"] == MODEL
    assert events[-1]["provider"] == "commandcode"
    assert events[-1]["selected"] == f"{MODEL}@commandcode"
    assert events[-1]["executable_binding"] == {
        "model": f"{MODEL}@commandcode",
        "base_url": "https://commandcode.sample/v1",
        "credential_env": "COMMANDCODE_API_KEY",
    }
    assert events[-1]["skipped"][0]["endpoint"] == f"{MODEL}@opencode"
    assert "model-upstream unavailable" in events[-1]["skipped"][0]["reason"]
    assert not any("risk ceiling exceeded" in reason for reason in reasons)


def test_commandcode_binding_reaches_final_spawn_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(tmp_path, monkeypatch, {})
    state = RuntimeState()
    for role in ("implement-standard", "implement-strong", "implement-hard", "implement-cheap"):
        state.set_available(role, False, "model-upstream unavailable")
    state.set_provider_unavailable("opencode", time.time() + 300, "provider held")
    registry = _configured_registry(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    router = Pipeline(
        registry=registry,
        mode="dynamic",
        state=state,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "route.jsonl",
    )
    decision = router.run(
        "route=implement: fix the bug; acceptance: pytest passes",
        session_id="endpoint-spawn",
    )
    assert decision.role == "implement-overflow"
    assert decision.model == MODEL

    route = decision_hook_record(decision)
    route["_runtime_state"] = state
    route["_executable_bindings"] = router.executable_bindings
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_gate_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(hook, "_ensure_track", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook, "_load_execution", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        hook,
        "_next_required_spawn_stage",
        lambda _track: SimpleNamespace(
            role="implement-overflow", kind="linear_spawn", stage_id="implement-overflow"
        ),
    )
    monkeypatch.setattr(hook, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(settlement, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook, "_append_log", lambda *args, **kwargs: None)

    hook.handle_pre_tool(
        {
            "sessionId": "endpoint-spawn",
            "toolName": "spawn_subagent",
            "toolInput": {
                "subagent_type": "implement-overflow",
                "prompt": "Apply the reviewed change.",
            },
            "cwd": str(tmp_path),
        },
        {},
    )

    output = json.loads(capsys.readouterr().out)
    final_input = output["hookSpecificOutput"]["updatedInput"]
    assert final_input["subagent_type"] == "implement-overflow"
    executable = registry.executable_bindings[final_input["model"]]
    assert executable.base_url == "https://api.commandcode.ai/provider/v1"
    assert executable.credential_env == "COMMANDCODE_API_KEY"


def test_commandcode_binding_reaches_nested_final_spawn_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(tmp_path, monkeypatch, {})
    state = RuntimeState()
    for role in ("implement-standard", "implement-strong", "implement-hard", "implement-cheap"):
        state.set_available(role, False, "model-upstream unavailable")
    state.set_provider_unavailable("opencode", time.time() + 300, "provider held")
    registry = _configured_registry(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    router = Pipeline(
        registry=registry,
        mode="dynamic",
        state=state,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "route.jsonl",
    )
    decision = router.run(
        "route=implement: fix the bug; acceptance: pytest passes",
        session_id="nested-endpoint-spawn",
    )
    assert decision.role == "implement-overflow"
    assert decision.model == MODEL

    route = decision_hook_record(decision)
    route["_runtime_state"] = state
    route["_executable_bindings"] = router.executable_bindings
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_gate_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(hook, "_ensure_track", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook, "_load_execution", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        hook,
        "_next_required_spawn_stage",
        lambda _track: SimpleNamespace(
            role="implement-overflow", kind="linear_spawn", stage_id="implement-overflow"
        ),
    )
    monkeypatch.setattr(hook, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(settlement, "record_stage_tx", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook, "_append_log", lambda *args, **kwargs: None)

    hook.handle_pre_tool(
        {
            "sessionId": "nested-endpoint-spawn",
            "toolName": "spawn_subagent",
            "toolInput": {
                "tool_name": "spawn_subagent",
                "tool_input": {
                    "subagent_type": "implement-overflow",
                    "prompt": "Apply the reviewed change.",
                },
            },
            "cwd": str(tmp_path),
        },
        {},
    )

    output = json.loads(capsys.readouterr().out)
    final_input = output["hookSpecificOutput"]["updatedInput"]
    assert final_input["tool_name"] == "spawn_subagent"
    assert "model" not in final_input
    nested_input = final_input["tool_input"]
    assert nested_input["subagent_type"] == "implement-overflow"
    assert nested_input["prompt"] == "Apply the reviewed change."
    executable = registry.executable_bindings[nested_input["model"]]
    assert executable.base_url == "https://api.commandcode.ai/provider/v1"
    assert executable.credential_env == "COMMANDCODE_API_KEY"


def test_endpoint_order_and_quota_skip_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "quota": {"windows": [{"used_percent": 95.0}]},
            }
        },
    )
    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )
    assert ok is True
    assert signal is None
    assert event["selected"] == f"{MODEL}@commandcode"
    assert event["skipped"] == [
        {
            "endpoint": f"{MODEL}@opencode",
            "provider": "opencode",
            "reason": "model-class: quota pressured | opencode",
        }
    ]


def test_quota_signal_ttl_classifies_fresh_pressure_and_staleness() -> None:
    now = time.time()

    def entry(used_percent: float, age: float) -> dict:
        return {
            "quota": {
                "fetched_at": now - age,
                "windows": [{"used_percent": used_percent}],
            }
        }

    assert availability._quota_acceptable(entry(50.0, 60), now=now) is True
    assert availability._quota_acceptable(entry(95.0, 60), now=now) is False
    assert (
        availability._quota_acceptable(entry(50.0, availability.QUOTA_SIGNAL_TTL + 1), now=now)
        is None
    )


def test_stale_quota_prefers_fresh_signal_free_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "quota": {
                    "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                    "windows": [{"used_percent": 50.0}],
                },
            }
        },
    )

    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )

    assert ok is True
    assert signal is None
    assert event["selected"] == f"{MODEL}@commandcode"
    assert "quota-signal-stale" in event["skipped"][0]["reason"]


def test_stale_quota_is_refreshed_once_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    key = f"{MODEL}@opencode"
    target = DiscoveryTarget(
        provider="opencode",
        label="OpenCode",
        base_url="https://opencode.sample/v1",
        env_key="OPENCODE_GO_API_KEY",
        catalog_ids=(MODEL,),
        raw_ids=(MODEL,),
        quota_probe="opencode",
    )
    _cache(
        tmp_path,
        monkeypatch,
        {
            key: {
                "status": "ok",
                "quota": {
                    "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                    "windows": [{"used_percent": 50.0}],
                },
            }
        },
    )
    fetch_calls: list[str] = []
    probe_calls: list[str] = []
    monkeypatch.setattr(availability.discovery, "load_targets", lambda: ({key: target}, []))
    monkeypatch.setattr(
        availability.discovery,
        "_default_fetch",
        lambda base_url, _bearer: (fetch_calls.append(base_url) or (200, [MODEL], None)),
    )
    monkeypatch.setitem(
        availability.discovery.QUOTA_PROBES,
        "opencode",
        lambda base_url, _bearer: (
            probe_calls.append(base_url) or {"plan": "test", "windows": [{"used_percent": 50.0}]}
        ),
    )

    first = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )
    second = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )

    assert first[0] is True and first[2]["selected"] == key
    assert second[0] is True and second[2]["selected"] == key
    assert fetch_calls == []
    assert probe_calls == [target.base_url]
    assert availability.discovery.FETCH_TIMEOUT == 4.0


def test_failed_lazy_refresh_uses_stale_reserve_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    key = f"{MODEL}@opencode"
    target = DiscoveryTarget(
        provider="opencode",
        label="OpenCode",
        base_url="https://opencode.sample/v1",
        env_key="OPENCODE_GO_API_KEY",
        catalog_ids=(MODEL,),
        raw_ids=(MODEL,),
        quota_probe="opencode",
    )
    _cache(
        tmp_path,
        monkeypatch,
        {
            key: {
                "status": "ok",
                "quota": {
                    "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                    "windows": [{"used_percent": 50.0}],
                },
            }
        },
    )
    monkeypatch.setattr(availability.discovery, "load_targets", lambda: ({key: target}, []))
    monkeypatch.setattr(
        availability.discovery, "_default_fetch", lambda *_args: (None, None, "network:Timeout")
    )
    monkeypatch.setitem(availability.discovery.QUOTA_PROBES, "opencode", lambda *_args: None)

    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )

    assert ok is True
    assert signal is None
    assert event["selected"] == f"{MODEL}@commandcode"
    assert "quota-signal-stale" in event["skipped"][0]["reason"]


def test_stale_quota_without_reserve_is_allowed_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    registry = _registry()
    meta = registry.provider_catalog[MODEL]
    registry = RoleRegistry(
        {"implement-overflow": registry.get("implement-overflow")},
        provider_catalog={MODEL: replace(meta, endpoints=meta.endpoints[:1])},
        executable_bindings={MODEL: registry.executable_bindings[MODEL]},
    )
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "quota": {
                    "fetched_at": time.time() - availability.QUOTA_SIGNAL_TTL - 1,
                    "windows": [{"used_percent": 50.0}],
                },
            }
        },
    )

    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", registry
    )

    assert ok is True
    assert signal is None
    assert event["selected"] == f"{MODEL}@opencode"
    assert "quota-signal-stale" in event["skipped"][0]["reason"]


def test_required_signal_missing_and_stale_advance_to_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    registry = _registry()
    meta = registry.provider_catalog[MODEL]
    endpoints = (replace(meta.endpoints[0], require_availability_signal=True), meta.endpoints[1])
    registry = registry.with_provider_catalog({MODEL: replace(meta, endpoints=endpoints)})

    for entry, expected in (
        ({}, "required availability signal missing"),
        (
            {"status": "ok", "fetched_at": time.time() - 8 * 86400},
            "required availability signal stale",
        ),
    ):
        _cache(tmp_path, monkeypatch, {f"{MODEL}@opencode": entry})
        ok, signal, event = availability._model_endpoint_availability(
            RuntimeState(), "implement-overflow", registry
        )
        assert ok is True
        assert signal is None
        assert event["selected"] == f"{MODEL}@commandcode"
        assert expected in event["skipped"][0]["reason"]


def test_all_endpoints_fail_feeds_high_risk_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state = RuntimeState()
    for role in ("implement-standard", "implement-strong", "implement-cheap"):
        state.set_available(role, False, "model-upstream unavailable")
    for provider in ("opencode", "commandcode"):
        state.set_provider_unavailable(provider, time.time() + 300, "infrastructure-timeout")

    config = REPO_CONFIG.read_text(encoding="utf-8")
    old = 'model = "gpt-6-astra"\nreasoning_effort = "max"\ndefault_capability_mode = "all"\nautonomy = "broad"'
    new = f'model = "{MODEL}"\nreasoning_effort = "max"\ndefault_capability_mode = "all"\nautonomy = "broad"'
    assert old in config
    config_path = tmp_path / "config.toml"
    config_path.write_text(config.replace(old, new, 1), encoding="utf-8")

    decision = Pipeline(
        registry=pipeline.load_registry(config_path),
        mode="dynamic",
        state=state,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "route.jsonl",
    ).run(
        "route=implement: migrate credential storage across the repository",
        session_id="endpoint-exhaustion",
        persist=True,
    )

    assert decision.role_spawnable is False
    assert decision.would_deny_edits is True
    assert decision.would_block_stop is True
    assert not any("infrastructure" in value for _, value in decision.stage_trace)
    assert any(
        warning == "BLOCKED: no capable+available model of class high; quota/human needed."
        for warning in decision.warnings
    )
    telemetry = json.loads((tmp_path / "route.jsonl").read_text(encoding="utf-8"))
    assert "endpoint_resolution" not in telemetry
    endpoint_records = [
        json.loads(line)
        for line in endpoint_resolution_path().read_text(encoding="utf-8").splitlines()
    ]
    hard_event = next(event for event in endpoint_records if event["model"] == MODEL)
    assert hard_event["schema"] == "endpoint-resolution-v1"
    assert hard_event["decision_id"] == decision.decision_id
    assert hard_event["selected"] is None
    assert len(hard_event["skipped"]) == 2
    assert all("infrastructure" in item["reason"] for item in hard_event["skipped"])


def test_legacy_single_binding_ignores_missing_runtime_credential() -> None:
    registry = pipeline.load_registry(REPO_CONFIG)
    role = registry.get("implement-hard")
    meta = registry.provider_catalog[role.model]
    assert meta.explicit_endpoints is False
    assert len(meta.endpoints) == 1
    available, signal = availability._role_availability(
        RuntimeState(), "implement-hard", registry, False
    )
    assert available is True
    assert signal is None


def _registry_for(model: str, role_name: str = "probe-role") -> RoleRegistry:
    catalog = load_provider_catalog()
    meta = catalog[model]
    role = Role(
        name=role_name,
        model=model,
        capability_mode="all",
        write=True,
        provider=meta.provider,
        provider_label=meta.provider_label,
        tier=meta.tier,
    )
    bindings = {}
    for endpoint in meta.endpoints:
        binding_id = endpoint.model_binding or model
        bindings[binding_id] = ExecutableModelBinding(
            binding_id, f"https://{endpoint.provider}.sample/v1", endpoint.credential_env
        )
    return RoleRegistry({role_name: role}, provider_catalog=catalog, executable_bindings=bindings)


def _all_endpoint_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        "OPENCODE_GO_API_KEY",
        "COMMANDCODE_API_KEY",
        "ZAI_API_KEY",
        "MINIMAX_API_KEY",
    ):
        monkeypatch.setenv(env, "test")


def test_rotate_balance_is_deterministic_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_endpoint_envs(monkeypatch)
    _cache(tmp_path, monkeypatch, {})
    registry = _registry()
    events = [
        availability._model_endpoint_availability(
            RuntimeState(), "implement-overflow", registry, session_id="rotate-session"
        )[2]
        for _ in range(2)
    ]
    assert events[0]["selected"] == events[1]["selected"]
    assert events[0]["balance_policy"] == "rotate"
    assert events[0]["rotate_start_index"] in (0, 1)

    event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", registry
    )[2]
    assert event["selected"] == f"{MODEL}@opencode"
    assert event["balance_policy"] == "rotate"
    assert event["rotate_start_index"] is None

    record = make_endpoint_resolution(
        decision_id="d-rotate", session_id="rotate-session", event=events[0]
    )
    assert record.balance_policy == "rotate"
    assert record.rotate_start_index == events[0]["rotate_start_index"]


def test_rotate_start_indices_spread_over_synthetic_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_endpoint_envs(monkeypatch)
    _cache(tmp_path, monkeypatch, {})
    registry = _registry()
    starts = set()
    for index in range(100):
        session_id = f"rotate-{index}"
        expected = int(hashlib.sha256(session_id.encode()).hexdigest()[:16], 16) % 2
        event = availability._model_endpoint_availability(
            RuntimeState(), "implement-overflow", registry, session_id=session_id
        )[2]
        assert event["rotate_start_index"] == expected
        starts.add(expected)
        expected_endpoint = f"{MODEL}@opencode" if expected == 0 else f"{MODEL}@commandcode"
        assert event["selected"] == expected_endpoint
    assert starts == {0, 1}


def test_ordered_models_start_at_declared_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_endpoint_envs(monkeypatch)
    _cache(tmp_path, monkeypatch, {})
    for model, primary in (
        ("minimax-m3", "minimax-m3@minimax"),
        ("glm-5.3-flash", "glm-5.3-flash@zai"),
    ):
        registry = _registry_for(model)
        for session_id in (None, "ordered-a", "ordered-b"):
            event = availability._model_endpoint_availability(
                RuntimeState(), "probe-role", registry, session_id=session_id
            )[2]
            assert event["selected"] == primary
            assert event["balance_policy"] == "ordered"
            assert event["rotate_start_index"] is None

    stripped = _registry()
    meta = stripped.provider_catalog[MODEL]
    stripped = stripped.with_provider_catalog({MODEL: replace(meta, balance="ordered")})
    for session_id in ("no-balance-a", "no-balance-b"):
        event = availability._model_endpoint_availability(
            RuntimeState(), "implement-overflow", stripped, session_id=session_id
        )[2]
        assert event["selected"] == f"{MODEL}@opencode"
        assert event["balance_policy"] == "ordered"


def test_rotated_held_endpoint_fails_over_with_typed_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_endpoint_envs(monkeypatch)
    registry = _registry()
    start_zero = next(
        f"failover-{index}"
        for index in range(100)
        if int(hashlib.sha256(f"failover-{index}".encode()).hexdigest()[:16], 16) % 2 == 0
    )
    start_one = next(
        f"failover-{index}"
        for index in range(100)
        if int(hashlib.sha256(f"failover-{index}".encode()).hexdigest()[:16], 16) % 2 == 1
    )
    _cache(
        tmp_path,
        monkeypatch,
        {
            f"{MODEL}@opencode": {
                "status": "ok",
                "quota": {"windows": [{"used_percent": 95.0}]},
            }
        },
    )
    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", registry, session_id=start_zero
    )
    assert ok is True and event["selected"] == f"{MODEL}@commandcode"
    assert event["skipped"][0]["endpoint"] == f"{MODEL}@opencode"
    assert "quota pressured" in event["skipped"][0]["reason"]

    state = RuntimeState()
    state.set_provider_unavailable("commandcode", time.time() + 300, "provider held")
    _cache(tmp_path, monkeypatch, {})
    ok, signal, event = availability._model_endpoint_availability(
        state, "implement-overflow", registry, session_id=start_one
    )
    assert ok is True and event["selected"] == f"{MODEL}@opencode"
    assert event["skipped"][0]["endpoint"] == f"{MODEL}@commandcode"
    assert "provider held" in event["skipped"][0]["reason"]


def test_endpoint_resolution_persistence_deduplicates_only_identical_skips(tmp_path: Path) -> None:
    from grokbuild.endpoint_resolution import persist_endpoint_resolution

    path = tmp_path / "resolutions.jsonl"
    base = make_endpoint_resolution(
        decision_id="dedup",
        session_id="s",
        event={"model": MODEL, "selected": MODEL, "provider": "opencode", "skipped": []},
    )
    persist_endpoint_resolution(base, path)
    persist_endpoint_resolution(replace(base, observed_at="later"), path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    changed = replace(base, skipped=({"endpoint": "x", "provider": "p", "reason": "different"},))
    persist_endpoint_resolution(changed, path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_freshest_quota_signal_wins_across_endpoint_and_provider_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale per-endpoint copy must not hide a fresh provider signal, nor the reverse."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "primary")
    monkeypatch.setenv("COMMANDCODE_API_KEY", "reserve")
    stale = time.time() - 4 * 86400
    fresh = time.time() - 60

    def entry(fetched_at: float, used_percent: float) -> dict:
        return {
            "status": "ok",
            "fetched_at": fetched_at,
            "quota": {"fetched_at": fetched_at, "windows": [{"used_percent": used_percent}]},
        }

    _cache(
        tmp_path,
        monkeypatch,
        {f"{MODEL}@opencode": entry(stale, 100.0), "opencode": entry(fresh, 0.0)},
    )
    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )
    assert ok is True and signal is None
    assert event["selected"] == f"{MODEL}@opencode"
    assert event["skipped"] == []

    _cache(
        tmp_path,
        monkeypatch,
        {f"{MODEL}@opencode": entry(stale, 0.0), "opencode": entry(fresh, 100.0)},
    )
    ok, signal, event = availability._model_endpoint_availability(
        RuntimeState(), "implement-overflow", _registry()
    )
    assert ok is True and event["selected"] == f"{MODEL}@commandcode"
    assert "quota pressured" in event["skipped"][0]["reason"]
    assert "stale" not in event["skipped"][0]["reason"]
