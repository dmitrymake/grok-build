#!/usr/bin/env python3
"""Deterministic tests for the dynamic Grok Build combine router engine.

No pytest. Stdlib only. Exercises the typed decision, role registry, feature
extractor, policy/scoring, runtime state, pipeline modes, redaction, and CLI.
"""

from __future__ import annotations

from _harness import make_check

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"
REPO_CONFIG = REPO_ROOT / "config" / "config.toml"

import contextlib
import io
import json
import os
import time
import tempfile


from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.decision import (  # noqa: E402
    SCHEMA_VERSION,
    ExecutionMember,
    ExecutionStage,
    RouteDecision,
    decision_from_dict,
    decision_to_dict,
)
from grokbuild.features import detect_override, extract_features  # noqa: E402
from grokbuild.compose import validate_execution  # noqa: E402
from grokbuild.policy import load_profiles  # noqa: E402
from grokbuild.roles import load_registry  # noqa: E402
from grokbuild.router import route_prompt  # noqa: E402
from grokbuild.stats import compute_stats  # noqa: E402
from grokbuild.state import (
    RuntimeState,
    default_log_path,
    default_state_path,
    load_state,
    save_state,
)
from grokbuild.persist import append_jsonl
from grokbuild.redact import redact, redact_text  # noqa: E402

FAILURES: list[str] = []


def ok(msg: str) -> None:
    print(f"ok   {msg}")


check = make_check(FAILURES)


def test_decision_roundtrip() -> None:
    d = RouteDecision(
        intent="security",
        role="security",
        model="glm-5.3",
        reasoning_effort="max",
        how="spawn_subagent security",
        block_tools=("search_replace", "write"),
        matches=(("security", ("rce",)),),
        strong=("rce",),
        has_strong=True,
        reason="matched",
        score=3.0,
        source="static",
        mode="static",
        stage_trace=(("extract", "ok"), ("policy", "pass")),
        observed_at="2026-08-15T00:00:00+00:00",
        second_opinion={
            "model": "gemini-3.7-flash",
            "provider": "commandcode",
            "reason": "low-confidence-routing",
            "confidence": 0.3,
        },
    )
    data = decision_to_dict(d)
    back = decision_from_dict(data)
    check(back == d, "decision round-trip is lossless")
    check(data["version"] == SCHEMA_VERSION, "decision carries schema version")
    check(back.second_opinion == d.second_opinion, "second opinion round-trip is lossless")
    legacy = dict(data)
    legacy.pop("second_opinion")
    check(
        decision_from_dict(legacy).second_opinion is None,
        "legacy decision missing second opinion loads as None",
    )

    try:
        decision_from_dict({**data, "version": 999})
        check(False, "unknown version must be rejected")
    except ValueError:
        ok("unknown decision version rejected")


def test_roles_validated_against_config() -> None:
    profile = load_profiles()["default"]
    check(profile.require_strong_for_gate is True, "global strong-only gate remains enabled")
    check(profile.gate_phrase_intents == ("implement",), "only implement phrases are profile-gated")
    check(profile.to_dict()["gate_phrase_intents"] == ["implement"], "phrase gate serializes")
    check(
        profile.barrier_stall_seconds == 1800.0
        and profile.to_dict()["barrier_stall_seconds"] == 1800.0,
        "default barrier stall profile is 1800 seconds",
    )
    check(
        profile.stop_block_limit == 3 and profile.to_dict()["stop_block_limit"] == 3,
        "default Stop block limit is three",
    )
    check(
        profile.debt_window_seconds == 86400.0
        and profile.to_dict()["debt_window_seconds"] == 86400.0,
        "default debt window is one day",
    )
    check(
        profile.consilium_after_failures == 3
        and profile.to_dict()["consilium_after_failures"] == 3,
        "default consilium threshold is three and serializes",
    )
    reg = load_registry(REPO_CONFIG)
    check(
        reg.get("general-purpose").model == "gpt-5.6-luna",
        "general-purpose infrastructure role pins Luna",
    )
    check(reg.get("security") is not None, "security role exists")
    visual = reg.get("visual-intake")
    visual_deep = reg.get("visual-intake-deep")
    check(
        visual is not None and visual.model == "minimax-m3" and visual.write is False,
        "visual-intake loads read-only",
    )
    check(
        visual is not None
        and visual.fallback == ("visual-intake-deep",)
        and visual.capabilities.vision is True,
        "visual-intake fallback and vision capability",
    )
    check(
        visual_deep is not None
        and visual_deep.model == "gpt-5.6-terra"
        and visual_deep.capabilities.vision is True,
        "visual-intake-deep loads with explicit vision",
    )
    check(
        (reg.get("security").model, reg.get("security").reasoning_effort) == ("glm-5.3", "max"),
        "security -> GLM @ max",
    )
    check(reg.get("security").write is False, "security role is read-only")
    check(
        (reg.get("implement-ops").model, reg.get("implement-ops").reasoning_effort)
        == ("glm-5.3", "high"),
        "implement-ops -> GLM @ high",
    )
    check(
        (reg.get("implement").model, reg.get("implement").reasoning_effort)
        == ("gpt-5.6-luna", "max"),
        "implement alias -> standard Luna @ max",
    )
    check(
        reg.canonical("implement") == "implement-standard", "implement alias resolves to standard"
    )
    check(
        (reg.get("implement-cheap").model, reg.get("implement-cheap").reasoning_effort)
        == ("gpt-5.6-luna", "high"),
        "implement-cheap -> Luna @ high",
    )
    check(
        (reg.get("implement-standard").model, reg.get("implement-standard").reasoning_effort)
        == ("gpt-5.6-luna", "max"),
        "implement-standard -> Luna @ max",
    )
    check(
        (reg.get("implement-hard").model, reg.get("implement-hard").reasoning_effort)
        == ("gpt-6-astra", "max"),
        "implement-hard -> Astra @ max",
    )
    check(
        (reg.get("explore-thorough").model, reg.get("explore-thorough").reasoning_effort)
        == ("gpt-5.6-luna", "max"),
        "explore-thorough -> Luna @ max",
    )
    check(
        reg.get("implement-overflow").model == "deepseek-v4-pro"
        and reg.get("implement-overflow").tier == "overflow",
        "overflow -> configured overflow tier",
    )
    check(reg.get("plan-hard").model == "gpt-6-astra", "plan-hard -> Astra read-only")
    check(reg.get("plan-hard").write is False, "plan-hard is read-only")
    check(reg.get("review").model == "gpt-5.6-sol", "review alias -> review-hard (Sol)")
    check(reg.canonical("review") == "review-hard", "review alias resolves to review-hard")
    check(reg.get("review-hard").model == "glm-5.3", "review-hard -> GLM")
    check(reg.get("review-hard").write is False, "review-hard read-only")
    check(reg.get("review-hard").review is True, "review-hard review-capable")
    check(reg.get("review-independent").model == "grok-4.6", "review-independent -> grok-4.6")
    check(reg.get("review-independent").write is False, "review-independent read-only")
    check(
        reg.get("review-independent").require_availability_signal is True,
        "grok review requires auth signal",
    )
    check(reg.get("expert-rescue").model == "deepseek-v4-pro", "expert-rescue -> DeepSeek")
    consilium = tuple(
        reg.get(name) for name in ("consilium-analyst", "consilium-challenger", "consilium-arbiter")
    )
    check(
        tuple(role.model for role in consilium) == ("glm-5.3", "deepseek-v4-pro", "gpt-5.6-sol"),
        "consilium roles load configured pins",
    )
    check(all(role.write is False for role in consilium), "consilium roles are read-only")
    check(
        tuple(role.provider for role in consilium) == ("zai", "opencode", "codex"),
        "consilium roles span three configured providers",
    )
    check(
        reg.get("implement-cheap-fallback").model == "deepseek-v4-flash",
        "implement-cheap-fallback -> Flash",
    )
    check(reg.get("implement-cheap-fallback").write is True, "implement-cheap-fallback writable")
    check(reg.get("security-verify").model == "grok-4.6", "security-verify -> grok-4.6")
    check(
        reg.get("security-verify").require_availability_signal is True,
        "security verifier requires auth signal",
    )
    check(reg.get("plan").model == "gpt-6-astra", "plan role model")
    check(
        (reg.get("explore").model, reg.get("explore").reasoning_effort) == ("gpt-5.6-luna", "high"),
        "explore -> Luna @ high",
    )
    check(
        not reg.validate_roles() or all(i["level"] != "error" for i in reg.validate_roles()),
        "role registry validates read-only invariants",
    )
    issues = reg.validate_intents(load_intents())
    errors = [i for i in issues if i["level"] == "error"]
    check(not errors, f"intents validate against config ({issues})")


def test_reasoning_effort_validation(tmp: Path) -> None:
    cases = (
        ("medium", "medium", "none"),
        ("turbo", "turbo", "warning"),
        ("", None, "error"),
        (3, None, "error"),
    )
    for index, (raw, expected, level) in enumerate(cases):
        value = json.dumps(raw)
        path = tmp / f"effort-{index}.toml"
        path.write_text(
            '[model."gpt-5.6-luna"]\nmodel="gpt-5.6-luna"\n'
            '[subagents.roles.explore]\nmodel="gpt-5.6-luna"\n'
            'default_capability_mode="read-only"\n'
            f"reasoning_effort={value}\n",
            encoding="utf-8",
        )
        reg = load_registry(path)
        check(
            reg.get("explore").reasoning_effort == expected,
            f"effort parsing preserves {raw!r} correctly",
        )
        issues = reg.validate_roles()
        check(
            level == "none" or any(i["level"] == level and i["role"] == "explore" for i in issues),
            f"effort {raw!r} yields {level}",
        )
    alias_path = tmp / "alias.toml"
    alias_path.write_text(
        '[model."gpt-5.6-sol"]\nmodel="gpt-5.6-sol"\n'
        '[subagents.roles.implement]\nmodel="gpt-5.6-sol"\nreasoning_effort="xhigh"\ndefault_capability_mode="all"\n',
        encoding="utf-8",
    )
    issues = load_registry(alias_path).validate_roles()
    check(
        any(i["level"] == "error" and i["role"] == "implement" for i in issues),
        "alias model/effort mismatch is an error",
    )


def test_features_and_overrides() -> None:
    spec = load_intents()
    feats = extract_features("найди RCE в demo-api", spec)
    check(feats.has_strong, "strong feature detected")
    check(dict(feats.strong).get("security") == ("rce",), "strong needle recorded")
    check(
        detect_override("route=security: review the code", spec["intents"]) == "security",
        "route= override",
    )
    check(
        detect_override("--route implement написать патч", spec["intents"]) == "implement",
        "--route override",
    )
    check(detect_override("@plan build adr", spec["intents"]) == "plan", "@ override")
    check(detect_override("просто security текст", spec["intents"]) is None, "no false override")
    feats2 = extract_features("route=security: ignore the rest", spec)
    check(feats2.override == "security", "extractor captures override token")


def test_router_intent_parity() -> None:
    spec = load_intents()
    cases = [
        "прошей роутер на x86",
        "прошить firmware и поднять vpn",
        "найди RCE в demo-api",
        "CVE-2026-1234 в vpn",
        "прошей роутер и найди CVE",
        "спроектируй архитектуру ai-console v0.2",
        "почему тесты красные, просто посмотри лог",
        "summarize this file",
        "как работает vpn, объясни",
        "<user_query>\nнайди RCE в demo-api\n</user_query>",
    ]
    # Pinned from the standalone classifier before WP-E6 removed its redundant API.
    expected_intents = [
        "implement",
        "implement",
        "security",
        "security",
        "security",
        "plan",
        None,
        None,
        None,
        "security",
    ]
    for prompt, expected in zip(cases, expected_intents):
        got = route_prompt(prompt, spec=spec, mode="static").intent
        check(got == expected, f"parity for {prompt!r}: engine={got} expected={expected}")


def test_state_and_circuit(tmp: Path) -> None:
    path = tmp / "state.json"
    state = RuntimeState(source_path=path)
    state.set_available("security", True)
    state.record_decision({"role": "security", "intent": "security"})
    save_state(state, path)
    loaded = load_state(path)
    check(loaded.status_for("security").quota_used == 1, "quota persists")
    check(loaded.is_available("security"), "role available by default")
    now = time.time()
    loaded.set_provider_unavailable("commandcode", until=now + 100.0, reason="quota", now=now)
    save_state(loaded, path)
    loaded = load_state(path)
    check(
        not loaded.is_provider_available("commandcode", now=now),
        "provider unavailability persists",
    )
    check(
        loaded.is_provider_available("commandcode", now=now + 200.0),
        "provider entry auto-recovers at expiry",
    )

    for _ in range(3):
        loaded.mark_failure("security", "down", threshold=3, cooldown=60)
    check(not loaded.is_available("security"), "circuit opens after threshold")
    check(loaded.circuit_open("security"), "circuit reports open")
    loaded.mark_success("security")
    check(loaded.is_available("security"), "success closes circuit")

    old = path.stat().st_mtime
    os.utime(path, (old - 24 * 3600 - 10, old - 24 * 3600 - 10))
    stale = load_state(path)
    check(stale.stale, "stale state is flagged")
    check(
        not stale.is_provider_available("commandcode", now=now),
        "provider quota survives stale role state",
    )


def test_history_bound(tmp: Path) -> None:
    state = RuntimeState(source_path=tmp / "s.json")
    for i in range(250):
        state.record_decision({"role": "security", "i": i})
    check(len(state.history) == 200, "history is bounded at 200")
    check(state.history[-1]["i"] == 249, "newest history kept")


def test_redaction() -> None:
    check(redact_text("key sk-abcdef1234567890 end") == "key [redacted] end", "sk- redacted")
    check(
        "password=supersecret" not in redact_text("x password=supersecret y"), "assignment redacted"
    )
    check("AKIAABCDEFGHIJKLMNOP" not in redact_text("AKIAABCDEFGHIJKLMNOP"), "aws key redacted")
    check(
        "supersecret" not in redact_text("fetch https://alice:supersecret@db.internal/x failed"),
        "url userinfo redacted even when the secret contains the letter s",
    )
    check(
        "sneakysecret" not in redact_text("GET /v1?token=sneakysecret&x=1"),
        "url query credential redacted even when it contains the letter s",
    )
    payload = redact({"token": "abc", "nested": {"secret": "zzz"}, "keep": "safe"})
    check(payload["token"] == "[redacted]", "secret key redacted")
    check(payload["nested"]["secret"] == "[redacted]", "nested secret redacted")
    check(payload["keep"] == "safe", "non-secret value kept")


def test_pipeline_modes(tmp: Path) -> None:
    spec = load_intents()
    base = tmp / "pipeline"
    state_path = base / "state.json"
    log_path = base / "route.jsonl"

    # static: no I/O, no enforcement.
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="static",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(d.intent == "security", "static classifies")
    check(d.role_available is None, "static does not consult state")
    check(not d.would_deny_edits and not d.would_block_stop, "static never enforces")
    check(not state_path.exists() and not log_path.exists(), "static performs no I/O")

    state = RuntimeState(source_path=state_path)
    state.set_available("security-verify", True, "verified test signal")
    save_state(state, state_path)

    # shadow: persists but never enforces.
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="shadow",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(d.intent == "security" and d.role_available is True, "shadow consults state")
    check(not d.would_deny_edits and not d.would_block_stop, "shadow never enforces")
    check(state_path.exists() and log_path.exists(), "shadow persists state and log")

    # dynamic: strong foreign-role edit/stop gate.
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(d.would_deny_edits and d.would_block_stop, "dynamic gates strong required pipeline")

    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="glm-5.3",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(d.would_deny_edits and d.would_block_stop, "same model still delegates")

    # Phrase-only implement is intent-gated without weakening security's strong-only rule.
    d = route_prompt(
        "напиши патч для demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(
        d.intent == "implement" and d.would_deny_edits and d.would_block_stop,
        "phrase-only implement is gated",
    )


def test_override_gates(tmp: Path) -> None:
    spec = load_intents()
    d = route_prompt(
        "route=security: no real needles here",
        spec=spec,
        mode="static",
        current_model="gpt-5.6-terra",
    )
    check(d.intent == "security" and d.override == "security", "override wins")
    check(d.reason == "override" and d.has_strong, "override counts as strong")


def test_enforce_hard_denies_phrase(tmp: Path) -> None:
    spec = load_intents()
    os.environ["GROK_ROUTE_ENFORCE"] = "1"
    try:
        d = route_prompt(
            "напиши патч для demo-api",
            spec=spec,
            mode="dynamic",
            current_model="deepseek-v4-pro",
            state_path=tmp / "s.json",
            log_path=tmp / "l.jsonl",
            persist=False,
        )
        check(d.intent == "implement" and d.would_deny_edits, "enforce gates phrase-only implement")
        check(d.would_block_stop, "enforce applies implement phrase stop gate")
    finally:
        os.environ.pop("GROK_ROUTE_ENFORCE", None)


def test_redacted_outcome_log(tmp: Path) -> None:
    spec = load_intents()
    log_path = tmp / "route.jsonl"
    route_prompt(
        "найди RCE в demo-api sk-abcdef1234567890",
        spec=spec,
        mode="shadow",
        current_model="gpt-5.6-terra",
        state_path=tmp / "s.json",
        log_path=log_path,
        persist=True,
    )
    text = log_path.read_text(encoding="utf-8")
    check("sk-abcdef1234567890" not in text, "secret never reaches the log")
    check("найди RCE" not in text, "prompt text never reaches the log")
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        check(isinstance(item, dict), "log lines are JSON objects")


def test_cli(tmp: Path) -> None:
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    try:
        import grokbuild.cli as cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["explain", "найди RCE в demo-api", "--json"])
        check(rc == 0, "cli explain exits 0")
        payload = json.loads(buf.getvalue())
        check(payload["intent"] == "security" and payload["mode"] == "static", "cli explain static")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(
                ["state", "set", "--role", "security", "--available", "false", "--reason", "down"]
            )
        check(rc == 0, "cli state set exits 0")
        saved = load_state(default_state_path())
        check(not saved.is_available("security"), "cli state set persists")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(
                [
                    "state",
                    "set",
                    "--role",
                    "implement",
                    "--quota-used",
                    "90",
                    "--quota-limit",
                    "100",
                ]
            )
        check(rc == 0, "cli quota set exits 0")
        saved = load_state(default_state_path()).status_for("implement")
        check(saved.quota_used == 90 and saved.quota_limit == 100, "cli quota set persists")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["state", "set", "--role", "implement", "--quota-used", "-1"])
        check(rc == 2, "negative quota-used rejected")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["state", "set", "--role", "implement", "--quota-used", "200"])
        check(rc == 2, "quota-used above limit rejected")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(
                ["provider", "commandcode", "quota-exhausted", "--until", "2030-01-07T00:00:00Z"]
            )
        check(
            rc == 0 and not load_state(default_state_path()).is_provider_available("commandcode"),
            "cli provider quota-exhausted persists",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["provider", "commandcode", "available"])
        check(
            rc == 0 and load_state(default_state_path()).is_provider_available("commandcode"),
            "cli provider available clears quota hold",
        )
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["provider", "commandcode", "quota-exhausted", "--until", "not-a-date"])
            check(False, "invalid provider --until rejected")
        except SystemExit as exc:
            check(exc.code == 2, "invalid provider --until rejected")

        append_jsonl(
            default_log_path(), {"intent": "security", "role": "security", "session_id": "s1"}
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["replay", "--last", "1"])
        check(rc == 0, "cli replay exits 0")
        replay = json.loads(buf.getvalue())
        check(replay["summary"]["total"] == 1, "cli replay summarises outcomes")

        log = default_log_path()
        Path(f"{log}.1").write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "telemetry_generation": 1,
                    "decision_id": "d1",
                    "event": "user_prompt_submit",
                    "intent": "implement",
                    "role": "implement-standard",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "version": 999,
                    "telemetry_generation": 1,
                    "decision_id": "old",
                    "event": "user_prompt_submit",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        append_jsonl(
            log,
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": 1,
                "decision_id": "d1",
                "event": "user_prompt_submit",
                "intent": "implement",
                "role": "implement-standard",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "max",
            },
        )
        append_jsonl(
            log,
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": 1,
                "decision_id": "d2",
                "event": "user_prompt_submit",
                "intent": None,
                "role": None,
                "model": "minimax-m3",
                "reasoning_effort": None,
            },
        )
        append_jsonl(
            log,
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": 1,
                "decision_id": "d3",
                "event": "user_prompt_submit",
                "intent": "plan",
                "role": "plan-hard",
                "model": "minimax-m3",
                "reasoning_effort": None,
            },
        )
        append_jsonl(
            log,
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": 1,
                "decision_id": "d4",
                "event": "user_prompt_submit",
                "intent": "explore",
                "role": "explore",
                "model": "minimax-m3",
                "reasoning_effort": "turbo",
            },
        )
        append_jsonl(
            log,
            {"version": SCHEMA_VERSION, "telemetry_generation": 1, "event": "user_prompt_submit"},
        )
        labels = tmp / "labels.json"
        labels.write_text(json.dumps({"d1": "uncertain"}), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["stats", "--json", "--labels", str(labels)])
        stats_payload = json.loads(buf.getvalue())
        check(
            rc == 0 and stats_payload["submits"]["all"] == 4,
            "cli stats reads rotations and deduplicates submits",
        )
        check(
            stats_payload["intent_routing"] == {"numerator": 3, "denominator": 4, "rate": 0.75}
            and stats_payload["model_divergence"]["numerator"] == 3
            and stats_payload["model_divergence"]["denominator"] == 3
            and stats_payload["model_divergence"]["rate"] == 1.0,
            "legacy model divergence remains and uses routed submits only",
        )
        check(
            stats_payload["pair_divergence"]["numerator"] == 3
            and stats_payload["pair_divergence"]["denominator"] == 3
            and stats_payload["pair_divergence"]["rate"] == 1.0,
            "pair divergence compares effective pairs over routed submits",
        )
        check(
            stats_payload["selected_pair_distribution"]
            == [
                {"model": "gpt-5.6-luna", "effort": "max", "count": 1, "rate": 1 / 3},
                {"model": "minimax-m3", "effort": "inherit", "count": 1, "rate": 1 / 3},
                {"model": "minimax-m3", "effort": "turbo", "count": 1, "rate": 1 / 3},
            ],
            "selected pair distribution preserves unknown efforts and renders null as inherit",
        )
        missing_stats = compute_stats(log=log, config_path=tmp / "missing-config.toml")
        check(
            missing_stats["pair_divergence"] is None,
            "pair divergence is null when config is missing",
        )
        check(
            stats_payload["ingestion"]["skipped"] >= 1
            and stats_payload["ingestion"]["malformed"] >= 1,
            "cli stats fails closed on unknown/malformed schemas",
        )
        check(
            stats_payload["labels"]["fpr"] is None,
            "cli stats label FPR is null without evaluable labels",
        )
    finally:
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("XDG_CACHE_HOME", None)


def test_consilium_provider_diversity_validation() -> None:
    reg = load_registry(REPO_CONFIG)
    stage = ExecutionStage(
        role="consilium",
        required=True,
        reason="test",
        kind="parallel_spawn",
        stage_id="consilium",
        members=(
            ExecutionMember("0", "review-hard", True, "one"),
            ExecutionMember("1", "plan-hard", True, "two"),
            ExecutionMember("2", "consilium-arbiter", True, "three"),
        ),
    )
    valid, warnings = validate_execution((stage,), reg)
    check(
        not valid and any("3 distinct providers" in warning for warning in warnings),
        "consilium validation rejects members sharing a provider",
    )


def test_profile_barrier_stall_defaults_and_overrides(tmp: Path) -> None:
    profile_path = tmp / "profiles.json"
    profile_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "default": {"mode": "dynamic"},
                    "custom": {
                        "barrier_stall_seconds": "12.5",
                        "stop_block_limit": "5",
                        "debt_window_seconds": "60.5",
                        "consilium_after_failures": "7",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    profiles = load_profiles(profile_path)
    check(
        profiles["default"].barrier_stall_seconds == 1800.0,
        "omitted barrier stall inherits default",
    )
    check(
        profiles["custom"].barrier_stall_seconds == 12.5
        and profiles["custom"].to_dict()["barrier_stall_seconds"] == 12.5,
        "custom barrier stall parses and serializes as float",
    )
    check(
        profiles["custom"].stop_block_limit == 5 and profiles["custom"].debt_window_seconds == 60.5,
        "custom Stop and debt knobs parse with configured types",
    )
    check(
        profiles["custom"].consilium_after_failures == 7
        and profiles["custom"].to_dict()["consilium_after_failures"] == 7,
        "custom consilium threshold parses and serializes as int",
    )


def main() -> int:
    spec = load_intents()
    check(spec.get("enforce_default") is False, "enforce_default stays false")

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_decision_roundtrip()
        test_roles_validated_against_config()
        test_consilium_provider_diversity_validation()
        test_profile_barrier_stall_defaults_and_overrides(tmp)
        test_reasoning_effort_validation(tmp)
        test_features_and_overrides()
        test_router_intent_parity()
        test_state_and_circuit(tmp)
        test_history_bound(tmp)
        test_redaction()
        test_pipeline_modes(tmp)
        test_override_gates(tmp)
        test_enforce_hard_denies_phrase(tmp)
        test_redacted_outcome_log(tmp)
        test_cli(tmp)

    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("router engine tests passed")
    return 0


if __name__ == "__main__":
    run_standalone(main)
