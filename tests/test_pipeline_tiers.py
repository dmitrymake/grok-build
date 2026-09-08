#!/usr/bin/env python3
"""Deterministic tests for the executor-tier pipeline and conductor resolver.

Covers Luna/Terra/Sol tier selection by scope/complexity/risk/testability,
failure escalation Luna->Terra->Sol, degraded fallback warnings, the Flash
constrained fallback (low-risk only), the non-spawnable deterministic verify
stage tracked separately, public role aliases, grok-4.6 read-only roles with no
credential binding, review/security read-only validation, review model
independence, and the typed ConductorHandoff resolver. Stdlib only; no live API.
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
import os
import tempfile
from dataclasses import replace
from unittest import mock


from grokbuild import hook as hook_route  # noqa: E402
from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.conductor import resolve_conductor  # noqa: E402
from grokbuild.decision import ConductorHandoff, STAGE_KINDS  # noqa: E402
from grokbuild.features import extract_features  # noqa: E402
import grokbuild.pipeline as pipeline  # noqa: E402
import grokbuild.gate as gate  # noqa: E402
from grokbuild.verifiers import _argv_has_shell_metachar  # noqa: E402
from grokbuild.pipeline import (  # noqa: E402
    Pipeline,
    is_verifier_command,
    parse_verifier_argv,
    resolve_verifier,
    review_independent_available,
    select_implement_role,
)
from grokbuild.compose import compose_execution, load_barrier_lenses, validate_execution  # noqa: E402

from grokbuild.policy import Profile, _merge_profile, gated_intents, load_profiles  # noqa: E402
from grokbuild.roles import (  # noqa: E402
    Role,
    RoleRegistry,
    load_provider_catalog,
    load_registry,
)
from grokbuild.router import route_prompt  # noqa: E402
from grokbuild.state import ExecutionStage, RuntimeState, save_state, load_state, default_state_path
from grokbuild.transactions import ensure_execution_tx, record_verify_tx, record_stage_tx

FAILURES: list[str] = []


check = make_check(FAILURES)


def test_verifier_metachar_controls_rejected() -> None:
    command = "runner 'line\nvalue'"
    check(
        parse_verifier_argv(command) is None,
        "pipeline verifier rejects a real newline in an argv token",
    )
    check(
        not is_verifier_command(command, "runner 'line\\nvalue'"),
        "pipeline verifier rejects newline command matching",
    )
    check(
        not _argv_has_shell_metachar((r"line\nvalue",)),
        "pipeline verifier allows a literal backslash-n pair",
    )


def test_conductor_handoff_roundtrip() -> None:
    h = ConductorHandoff(
        model="deepseek-v4-pro", provider="opencode", degraded=False, credential_present=True
    )
    back = ConductorHandoff.from_dict(h.to_dict())
    check(back == h, "ConductorHandoff round-trip is lossless")
    check(not back.degraded and back.model == "deepseek-v4-pro", "handoff carries conductor")


def test_conductor_resolution() -> None:
    catalog = load_provider_catalog()
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        (home / ".grok").mkdir()
        (home / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
        env = {
            "HOME": str(home),
            "ZAI_API_KEY": "x",
            "MINIMAX_API_KEY": "x",
            "COMMANDCODE_API_KEY": "x",
            "OPENCODE_GO_API_KEY": "x",
            "CODEX_SUB_PROXY_BEARER": "x",
        }

        state = RuntimeState()
        handoff = resolve_conductor(state=state, env=env, catalog=catalog)
        check(
            handoff.model == "glm-5.3-flash" and handoff.provider == "zai" and not handoff.degraded,
            "default conductor is configured security-Flash via Z.AI",
        )

        state.set_available("glm-5.3-flash", False, "model unavailable")
        handoff = resolve_conductor(state=state, env=env, catalog=catalog)
        check(
            handoff.model == "gemini-3.7-flash"
            and handoff.provider == "commandcode"
            and handoff.degraded,
            "degrade to Gemini when Flash is unavailable",
        )

        state.set_available("glm-5.3-flash", True, "recovered")
        state.set_provider_unavailable("zai", until=200.0, reason="quota", now=100.0)
        with mock.patch("grokbuild.transactions.time.time", return_value=100.0):
            handoff = resolve_conductor(state=state, env=env, catalog=catalog)
        check(handoff.model == "gemini-3.7-flash", "Z.AI quota hold skips the GLM conductor")

        state.set_available("gemini-3.7-flash", False, "Command Code unavailable")
        with mock.patch("grokbuild.transactions.time.time", return_value=100.0):
            handoff = resolve_conductor(state=state, env=env, catalog=catalog)
        check(handoff.model == "grok-4.6", "chain ends at grok-4.6")


def test_second_opinion_matrix() -> None:
    registry = load_registry(REPO_CONFIG)
    base = load_profiles()["default"]
    old = os.environ.get("COMMANDCODE_API_KEY")
    os.environ["COMMANDCODE_API_KEY"] = "test"
    try:

        def routed(weight: float, state: RuntimeState, prompt: str = "напиши патч для demo-api"):
            profile = replace(
                base, name="opinion", strong_weight=weight, phrase_weight=weight, topic_weight=0.0
            )
            return Pipeline(
                registry=registry,
                profiles={"opinion": profile},
                profile_name="opinion",
                mode="dynamic",
                state=state,
            ).run(prompt, persist=False)

        eligible = routed(2.9, RuntimeState())
        check(
            eligible.confidence == 0.29
            and eligible.second_opinion
            == {
                "model": "gemini-3.7-flash",
                "provider": "commandcode",
                "reason": "low-confidence-routing",
                "confidence": 0.29,
            },
            "0.29 confidence requests eligible Command Code second opinion",
        )

        exhausted = RuntimeState()
        exhausted.set_provider_unavailable("commandcode", until=200.0, reason="quota", now=100.0)
        with mock.patch("grokbuild.transactions.time.time", return_value=100.0):
            denied = routed(2.9, exhausted)
        check(
            denied.second_opinion
            == {
                "model": None,
                "provider": "commandcode",
                "reason": "provider-unavailable",
                "confidence": 0.29,
            },
            "0.29 confidence records unavailable second opinion",
        )
        check(
            routed(3.0, RuntimeState()).second_opinion is None,
            "0.30 confidence (one strong needle) has no second opinion",
        )
        check(
            routed(2.9, RuntimeState(), "route=implement: напиши патч для demo-api").second_opinion
            is None,
            "override confidence 1.0 has no second opinion",
        )
    finally:
        if old is None:
            os.environ.pop("COMMANDCODE_API_KEY", None)
        else:
            os.environ["COMMANDCODE_API_KEY"] = old


def test_no_intent_policy_shape() -> None:
    """Unmatched prompts are a deliberate observe-only decision, not a guess."""

    def shape(decision, reason: str, label: str) -> None:
        check(decision.intent is None and decision.reason == reason, f"{label}: intent/reason")
        check(decision.role is None and decision.model is None, f"{label}: no role or model")
        check(decision.execution == (), f"{label}: empty execution")
        check(decision.write_policy == "observe", f"{label}: observe write policy")
        check(decision.task_class == "general" and decision.risk == "low", f"{label}: class/risk")
        check(decision.score == 0.0 and decision.confidence == 0.0, f"{label}: zero score")
        check(decision.second_opinion is None, f"{label}: no second opinion")
        check(decision.block_tools == (), f"{label}: no blocked tools")
        check(
            not decision.enforce
            and not decision.would_deny_edits
            and not decision.would_block_stop,
            f"{label}: no gates",
        )
        check(decision.warnings == () and decision.fallbacks == (), f"{label}: no warnings")

    shape(route_prompt("что нового в этом проекте?", mode="static"), "no_intent", "no_intent")
    high_threshold = {
        "p": Profile(
            name="p",
            mode="dynamic",
            strong_weight=10.0,
            phrase_weight=1.0,
            topic_weight=1.0,
            min_score=100.0,
        )
    }
    shape(
        route_prompt(
            "напиши патч для demo-api",
            spec=load_intents(),
            profiles=high_threshold,
            profile_name="p",
            mode="static",
        ),
        "below_threshold",
        "below_threshold",
    )


def test_destructive_syntax_and_data_noun_regression() -> None:
    """Destructive syntax must classify as implement-ops; данный is not a target."""
    registry = load_registry(REPO_CONFIG)
    ops_cases = (
        "rm -rf /srv/application",
        "drop table users",
        "truncate database audit",
        "format disk",
        "удали данные из таблицы",
        "удали базу данных в проде",
        # Edge punctuation must not detach a verb or an exact data noun.
        "удали данные, пожалуйста",
        "удали, пожалуйста, таблицы в проде",
    )
    for prompt in ops_cases:
        decision = route_prompt(prompt, spec=load_intents(), mode="static", registry=registry)
        check(
            decision.intent == "implement"
            and decision.risk == "high"
            and decision.role == "implement-ops",
            f"destructive syntax routes to implement-ops ({prompt!r} -> "
            f"{decision.intent}/{decision.risk}/{decision.role})",
        )

    plain = route_prompt(
        "удали данный импорт", spec=load_intents(), mode="static", registry=registry
    )
    check(
        plain.intent == "implement" and plain.risk != "high" and plain.role == "implement-standard",
        f"данный (adjective) is not a data target ({plain.risk}/{plain.role})",
    )
    base = route_prompt("удали базовый кэш", spec=load_intents(), mode="static", registry=registry)
    check(
        base.intent == "implement" and base.risk != "high" and base.role != "implement-ops",
        f"bare баз prefix is not a target ({base.risk}/{base.role})",
    )
    polite = route_prompt(
        "удали, пожалуйста, импорт", spec=load_intents(), mode="static", registry=registry
    )
    check(
        polite.intent == "implement"
        and polite.risk == "medium"
        and polite.role == "implement-standard",
        f"a comma'd verb without a target stays off the ops branch ({polite.risk}/{polite.role})",
    )


def test_tier_selection() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    feats = extract_features("прошей роутер на x86", load_intents())
    role, reasons, degraded = select_implement_role("low", "high", feats, state, registry, None)
    check(role == "implement-ops" and not degraded, f"firmware/destructive -> ops ({role})")

    feats = extract_features("напиши патч для demo-api", load_intents())
    role, reasons, degraded = select_implement_role("low", "low", feats, state, registry, None)
    check(role == "implement-cheap" and not degraded, f"low scope/risk -> cheap ({role})")

    feats = extract_features("рефактор " + " ".join(["кода"] * 60), load_intents())
    role, reasons, degraded = select_implement_role("high", "medium", feats, state, registry, None)
    check(role == "implement-hard", f"high complexity -> hard ({role})")


def test_failure_escalation() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.mark_failure("implement-cheap", "x")
    feats = extract_features("напиши патч для demo-api", load_intents())
    role, reasons, degraded = select_implement_role("low", "low", feats, state, registry, None)
    check(role == "implement-standard", f"cheap failure escalates to standard ({role})")

    state.mark_failure("implement-standard", "x")
    role, reasons, degraded = select_implement_role("low", "low", feats, state, registry, None)
    check(role == "implement-strong", f"standard failure escalates to strong ({role})")


def test_degraded_fallback() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.set_available("implement-hard", False, "down")
    state.set_available("implement-standard", False, "down")
    state.set_available("implement-strong", False, "down")
    feats = extract_features("repo-wide migration", load_intents())
    role, reasons, degraded = select_implement_role("high", "high", feats, state, registry, None)
    check(role == "implement-cheap" and degraded, f"hard down degrades down ({role})")
    check(any("fallback" in r for r in reasons), "degraded fallback warning")


def test_overflow_after_primary_tiers() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    for role_name in (
        "implement-cheap",
        "implement-standard",
        "implement-strong",
        "implement-hard",
    ):
        state.set_available(role_name, False, "Codex down")
    feats = extract_features("напиши тест для demo-api", load_intents())
    role, reasons, degraded = select_implement_role(
        "low", "low", feats, state, registry, None, verifier_known=True
    )
    check(
        role == "implement-overflow" and degraded, f"Codex down -> overflow before Flash ({role})"
    )
    overflow = registry.get(role)
    check(
        overflow.model == "deepseek-v4-pro" and overflow.provider == "opencode",
        "overflow resolves to configured overflow model",
    )


def test_flash_fallback_only_low_risk() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    for r in (
        "implement-cheap",
        "implement-standard",
        "implement-strong",
        "implement-hard",
        "implement-overflow",
    ):
        state.set_available(r, False, "down")

    feats = extract_features("напиши тест для demo-api", load_intents())
    role, reasons, degraded = select_implement_role(
        "low", "low", feats, state, registry, None, verifier_known=True
    )
    check(
        role == "implement-cheap-fallback" and degraded,
        f"Flash is last-resort writable fallback, never explore-as-implement ({role})",
    )

    state.set_available("implement-ops", False, "down")
    feats = extract_features("прошей роутер на x86", load_intents())
    role, reasons, degraded = select_implement_role("low", "high", feats, state, registry, None)
    check(role is None, "Flash fallback never for high-risk work")


def test_verify_stage_tracked_separately(tmp: Path) -> None:
    path = tmp / "state.json"
    state = RuntimeState(source_path=path)
    stages = (
        ExecutionStage("implement-hard", True, "implementation", ()),
        ExecutionStage("review-independent", True, "review", ()),
        ExecutionStage(
            "verify",
            True,
            "deterministic verification",
            (),
            kind="verify",
            command="./tests/grok-route-test.sh",
            stage_id="verify/0",
        ),
        ExecutionStage(
            "verify",
            True,
            "deterministic verification",
            (),
            kind="verify",
            command="./tests/grok-combine-install-test.sh",
            stage_id="verify/1",
        ),
    )
    track = state.set_execution("d1", "s1", 1, stages)
    check(
        track.required_roles() == ["implement-hard", "review-independent"],
        "verify step not a spawnable role",
    )
    check(
        track.required_verify_steps() == ["verify/0", "verify/1"], "verify step tracked as verify"
    )
    track.completed.extend(["implement-hard", "review-independent"])
    check(not track.all_required_completed(), "verify pending keeps gate up")
    save_state(state, path)
    record_verify_tx(path, "d1", "verify/0", True)
    reconciled_stages = (
        *stages[:2],
        replace(stages[2], reason="deterministic verification (reconciled)"),
        stages[3],
    )
    ensure_execution_tx(path, "d1", "s1", 1, [stage.to_dict() for stage in reconciled_stages])
    loaded = load_state(path)
    check(
        "verify/0" in loaded.get_execution("d1").verified,
        "reconciliation preserves verify stage identity",
    )
    check(
        loaded.get_execution("d1").next_verify_step() == "verify/1",
        "second verify remains due after first",
    )
    record_verify_tx(path, "d1", "verify/1", True)
    loaded = load_state(path)
    check(loaded.get_execution("d1").all_required_completed(), "verify completion lifts gate")
    check(
        {"verify/0", "verify/1"}.issubset(loaded.get_execution("d1").verified),
        "verify tracked in verified, not completed",
    )


def test_confirmation_batch_kind_is_spawnable() -> None:
    stage = ExecutionStage(
        "verifier-planner",
        True,
        "confirmation",
        kind="confirmation_batch",
        stage_id="confirmation-batch",
    )
    check("confirmation_batch" in STAGE_KINDS, "confirmation batch is a declared stage kind")
    check(stage.spawnable, "confirmation batch is an explicit spawnable stage kind")


def test_confirmation_batch_profile_serialization_and_inheritance() -> None:
    profiles = load_profiles()
    default = profiles["default"]
    confirmation = profiles["confirmation"]
    inherited = _merge_profile("child", {}, confirmation)
    check(default.confirmation_batch is None, "default does not enable confirmation batches")
    check(
        "confirmation_batch" not in default.to_dict(),
        "default serialization omits the unset confirmation flag",
    )
    check(
        confirmation.confirmation_batch is True
        and confirmation.to_dict()["confirmation_batch"] is True,
        "the confirmation profile carries and serializes the opt-in flag",
    )
    check(inherited.confirmation_batch is True, "profile merging inherits the confirmation flag")


def test_attributed_success_composes_confirmation_after_first_verify() -> None:
    stages = compose_execution(
        "implement",
        "low",
        "low",
        "implement-cheap",
        "implement-cheap",
        verify_commands=("verify-one", "verify-two"),
        profile=load_profiles()["confirmation"],
    )
    first_verify = next(index for index, stage in enumerate(stages) if stage.kind == "verify")
    confirmation = stages[first_verify + 1]
    check(confirmation.kind == "confirmation_batch", "confirmation follows the first verify slot")
    check(confirmation.role == "verifier-planner", "confirmation uses a verifier-family role")
    check(
        "second verification pass; partially new checks after first attributed success"
        in confirmation.reason,
        "confirmation policy states its second-pass purpose",
    )
    check(
        'attribute_evidence == "attributed"' in confirmation.reason,
        "confirmation policy preserves the future attributed-evidence runtime gate",
    )
    check(
        sum(stage.kind == "confirmation_batch" for stage in stages) == 1,
        "one confirmation stage is composed",
    )


def test_default_confirmation_composition_has_structural_parity() -> None:
    default = load_profiles()["default"]
    stages = compose_execution(
        "implement",
        "low",
        "low",
        "implement-cheap",
        "implement-cheap",
        verify_commands=("verify-one",),
        profile=default,
    )
    expected = (
        ExecutionStage(
            "explore",
            True,
            "cheap read-only reconnaissance before implementation",
            (),
            slot="recon",
        ),
        ExecutionStage("implement-cheap", True, "implementation (required)", (), slot="impl"),
        ExecutionStage(
            "verify",
            True,
            "deterministic verification",
            (),
            kind="verify",
            command="verify-one",
            stage_id="verify/0",
            slot="verify/0",
        ),
    )
    check(stages == expected, "default composition remains structurally identical")
    check(
        stages
        == compose_execution(
            "implement",
            "low",
            "low",
            "implement-cheap",
            "implement-cheap",
            verify_commands=("verify-one",),
        ),
        "an omitted profile and the default profile compose identically",
    )


def test_aliases() -> None:
    reg = load_registry(REPO_CONFIG)
    check(reg.canonical("implement") == "implement-standard", "implement -> standard")
    check(reg.canonical("review") == "review-hard", "review -> hard (internal)")
    check(reg.canonical("plan") == "plan-hard", "plan -> hard")
    check(reg.canonical("security") == "security", "security stays canonical")


def test_conductor_flash_catalog_has_only_the_criterion_role_pin() -> None:
    reg = load_registry(REPO_CONFIG)
    flash = reg.provider_catalog.get("glm-5.3-flash")
    check("glm-5.3-flash" in reg.known_models, "configured security-Flash is a configured model")
    check(
        flash is not None and flash.provider == "zai", "configured security-Flash provider is Z.AI"
    )
    check(
        flash is not None and flash.capabilities.vision is True,
        "configured security-Flash has explicit vision capability",
    )
    pinned = {role.name for role in reg._roles.values() if role.model == "glm-5.3-flash"}
    check(
        pinned == {"criterion-judge", "judge-independent", "verifier-planner"},
        f"configured security-Flash pins only the criterion judge ({pinned})",
    )


def test_grok_roles_read_only_no_binding() -> None:
    reg = load_registry(REPO_CONFIG)
    check("gemini-3.7-flash" in reg.known_models, "Gemini conductor is a configured model")
    check(
        reg.provider_catalog["gemini-3.7-flash"].provider == "commandcode",
        "Gemini conductor provider is Command Code",
    )
    expected = {
        "review-independent": ("grok-4.6", "xai", True),
        "expert-rescue": ("deepseek-v4-pro", "opencode", False),
        "security-verify": ("grok-4.6", "xai", True),
    }
    for name, (model, provider, signal) in expected.items():
        role = reg.get(name)
        check(role is not None and role.write is False, f"{name} read-only")
        check(role.model == model and role.provider == provider, f"{name} uses {model}/{provider}")
        check(role.require_availability_signal is signal, f"{name} availability-signal binding")
    # No [model."grok-4.6"] block exists in config.toml.
    check(
        "grok-4.6" not in reg.known_models,
        "grok-4.6 has no config model block (no api_key/env_key/base_url)",
    )
    review_hard = reg.get("review-hard")
    check(review_hard is not None and review_hard.write is False, "review-hard read-only")
    check(
        review_hard.model == "glm-5.3" and review_hard.provider == "zai",
        "review-hard uses glm-5.3/zai",
    )
    check(review_hard.review is True, "review-hard is review-capable")
    check(reg.get("implement-cheap-fallback").write is True, "implement-cheap-fallback is writable")
    check(
        reg.get("implement-cheap-fallback").model == "deepseek-v4-flash",
        "implement-cheap-fallback uses Flash",
    )


def test_read_only_registry_validation() -> None:
    reg = RoleRegistry(
        {
            "security": Role(
                name="security",
                model="glm-5.3",
                capability_mode="all",
                write=True,
                security=True,
            ),
        },
        known_models=frozenset({"glm-5.3"}),
    )
    errors = [i for i in reg.validate_roles() if i["level"] == "error"]
    check(any("read-only" in e["msg"] for e in errors), "writable security role is rejected")


def test_review_independence_validation() -> None:
    stages = (
        ExecutionStage("security", True, "analysis", ()),
        ExecutionStage("implement-hard", True, "implementation", ()),
        ExecutionStage("review-independent", True, "review", ()),
        ExecutionStage("security-verify", True, "verify", ()),
    )
    reg = RoleRegistry(
        {
            "security": Role(
                name="security",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
            ),
            "implement-hard": Role(
                name="implement-hard", model="grok-4.6", capability_mode="all", write=True
            ),
            "review-independent": Role(
                name="review-independent",
                model="grok-4.6",
                capability_mode="read-only",
                write=False,
                review=True,
            ),
            "security-verify": Role(
                name="security-verify",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
            ),
        },
        known_models=frozenset({"glm-5.3", "grok-4.6"}),
    )
    valid, warnings = validate_execution(stages, reg)
    check(
        not valid and any("family-independent" in w for w in warnings),
        "review cannot share model family with implement stage",
    )

    distinct = RoleRegistry(
        {
            "implement-hard": Role(
                name="implement-hard", model="gpt-5.6-sol", capability_mode="all", write=True
            ),
            "review-independent": Role(
                name="review-independent",
                model="grok-4.6",
                capability_mode="read-only",
                write=False,
                review=True,
            ),
        },
        known_models=frozenset({"gpt-5.6-sol", "grok-4.6"}),
    )
    valid, warnings = validate_execution((stages[1], stages[2]), distinct)
    check(valid, f"independent review may share Grok with conductor, not implementer ({warnings})")


def test_review_independence_uses_model_family() -> None:
    base = next(iter(load_provider_catalog().values()))
    catalog = {
        "gpt-5.6-sol": replace(base, family="gpt-5.6"),
        "gpt-5.6-luna": replace(base, family="gpt-5.6"),
    }
    registry = RoleRegistry(
        {
            "implement-hard": Role(name="implement-hard", model="gpt-5.6-sol", write=True),
            "review-independent": Role(
                name="review-independent",
                model="gpt-5.6-luna",
                write=False,
                review=True,
            ),
        },
        provider_catalog=catalog,
    )
    valid, warnings = validate_execution(
        (
            ExecutionStage("implement-hard", True, "implementation"),
            ExecutionStage("review-independent", True, "review"),
        ),
        registry,
    )
    check(not valid, f"sol implementer and luna reviewer share a family ({warnings})")
    check(
        any("family-independent" in warning for warning in warnings), "family conflict is explicit"
    )


def test_unpinned_independent_review_warns_but_allows() -> None:
    registry = RoleRegistry(
        {
            "implement-hard": Role(name="implement-hard", model="gpt-5.6-sol", write=True),
            "review-independent": Role(
                name="review-independent", model=None, write=False, review=True
            ),
        }
    )
    valid, warnings = validate_execution(
        (
            ExecutionStage("implement-hard", True, "implementation"),
            ExecutionStage("review-independent", True, "review"),
        ),
        registry,
    )
    check(valid, f"unpinned independence remains allowed ({warnings})")
    check(
        "review independence unverifiable: unpinned model" in warnings,
        "unpinned independent review emits the required warning",
    )


def _internal_review_registry(review_model: str = "gpt-5.6-sol") -> RoleRegistry:
    return RoleRegistry(
        {
            "implement-hard": Role(name="implement-hard", model="gpt-5.6-sol", write=True),
            "review-hard": Role(name="review-hard", model=review_model, write=False, review=True),
            "review-independent": Role(
                name="review-independent", model="grok-4.6", write=False, review=True
            ),
        },
        provider_catalog=load_provider_catalog(),
    )


def test_internal_review_alternatives_are_inert() -> None:
    stages, warnings = pipeline._soft_review_independence(
        (
            ExecutionStage("implement-hard", True, "implementation"),
            ExecutionStage("review-hard", True, "review", alternatives=("review-independent",)),
        ),
        _internal_review_registry(),
    )
    check(stages[1].role == "review-hard", f"alternative does not replace role ({stages})")
    check(any("degraded-review" in warning for warning in warnings), "degraded review is visible")


def test_internal_review_same_family_marks_degraded_but_allows() -> None:
    stages = (
        ExecutionStage("implement-hard", True, "implementation"),
        ExecutionStage("review-hard", True, "review"),
    )
    normalized, marker_warnings = pipeline._soft_review_independence(
        stages, _internal_review_registry()
    )
    valid, warnings = validate_execution(normalized, _internal_review_registry())
    combined = marker_warnings + warnings
    check(valid and normalized == stages, f"degraded internal review remains legal ({combined})")
    check(any("degraded-review" in warning for warning in combined), "degraded marker is visible")
    check(
        any("reason_code=degraded_review_same_family" in warning for warning in combined),
        "degraded marker carries a telemetry reason code",
    )


def test_internal_review_different_family_is_clean() -> None:
    stages = (
        ExecutionStage("implement-hard", True, "implementation"),
        ExecutionStage("review-hard", True, "review"),
    )
    normalized, warnings = pipeline._soft_review_independence(
        stages, _internal_review_registry("grok-4.6")
    )
    valid, validation_warnings = validate_execution(
        normalized, _internal_review_registry("grok-4.6")
    )
    check(valid and normalized == stages, f"different-family review remains unchanged ({warnings})")
    check(not warnings and not validation_warnings, "different-family review has no marker")


def test_degraded_review_marker_reaches_route_telemetry() -> None:
    decision = route_prompt(
        "Сделай repo-wide refactor публичного API с миграцией",
        spec=load_intents(),
        mode="static",
        state=RuntimeState(),
        registry=load_registry(REPO_CONFIG),
        persist=False,
        workspace_root=str(REPO_ROOT),
    )
    outcome = pipeline.decision_outcome(decision)
    markers = [warning for warning in outcome["warnings"] if "independent review" in warning]
    check(
        markers,
        f"independent-review availability warning is persisted in telemetry ({outcome['warnings']})",
    )
    check(
        not any("degraded-review" in warning for warning in outcome["warnings"]),
        "independent review fallback is not mislabeled as same-family degradation",
    )


def test_pipeline_compat() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)
    d = route_prompt(
        "прошей роутер на x86",
        spec=spec,
        mode="static",
        workspace_root=ws,
        registry=load_registry(REPO_CONFIG),
    )
    check(d.role == "implement-ops", "static picks ops for destructive implement")
    check(
        [s.role for s in d.execution]
        == ["explore", "implement-ops", "review-hard", "verify", "verify"],
        "low firmware pipeline starts with recon, stays ops, and degrades to review-hard",
    )
    d2 = route_prompt("найди RCE в demo-api", spec=spec, mode="static", workspace_root=ws)
    check(
        [s.role for s in d2.execution]
        == ["security", "implement-hard", "security-verify", "verify", "verify"],
        "secure stages order (no grok review signal in static)",
    )
    check(
        any("independent review" in w for w in d2.warnings),
        "high-risk degrades with a warning when independent review is unavailable",
    )


def test_review_hard_internal_review() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)
    registry = load_registry(REPO_CONFIG)
    # Medium complexity, non-high-risk standard (Terra) implementation.
    prompt = "рефактор " + " ".join(["кода"] * 40)
    d = route_prompt(prompt, spec=spec, mode="static", workspace_root=ws, registry=registry)
    check(
        d.intent == "implement" and d.role == "implement-standard",
        f"Terra standard selected ({d.role})",
    )
    check(
        d.complexity == "medium" and d.risk != "high",
        f"medium complexity, non-high risk ({d.complexity}/{d.risk})",
    )
    check(
        [s.role for s in d.execution]
        == ["recon", "implement-standard", "review-hard", "verify", "verify"],
        f"recon barrier -> Terra -> review-hard -> verify ({[s.role for s in d.execution]})",
    )
    check(
        d.execution[0].required
        and d.execution[0].kind == "parallel_spawn"
        and [m.role for m in d.execution[0].members] == ["explore", "explore", "explore-thorough"],
        "medium implementation uses the default three-member recon barrier",
    )


def test_hard_tier_medium_risk_gets_review() -> None:
    spec = load_intents()
    prompt = "route=implement: рефактор " + " ".join(["кода"] * 80)
    decision = route_prompt(
        prompt,
        spec=spec,
        mode="static",
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    check(
        decision.complexity == "high"
        and decision.risk == "medium"
        and decision.role == "implement-hard",
        f"high complexity, medium risk selects hard ({decision.complexity}/{decision.risk}/{decision.role})",
    )
    check(
        any(stage.role == "review-hard" for stage in decision.execution),
        "hard implementation receives review-hard",
    )


def test_overflow_medium_complexity_gets_review() -> None:
    state = RuntimeState()
    for role_name in (
        "implement-cheap",
        "implement-standard",
        "implement-strong",
        "implement-hard",
    ):
        state.set_available(role_name, False, "down")
    decision = route_prompt(
        "route=implement: рефактор " + " ".join(["кода"] * 40),
        spec=load_intents(),
        mode="dynamic",
        state=state,
        persist=False,
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    check(
        decision.complexity == "medium" and decision.role == "implement-overflow",
        f"medium degraded route selects overflow ({decision.complexity}/{decision.role})",
    )
    check(
        any(stage.role == "review-hard" for stage in decision.execution),
        "overflow implementation receives review-hard",
    )


def test_review_unavailable_without_verifier_observe_only() -> None:
    state = RuntimeState()
    state.set_available("review-hard", False, "down")
    decision = route_prompt(
        "route=implement: рефактор " + " ".join(["кода"] * 40),
        spec=load_intents(),
        mode="dynamic",
        state=state,
        persist=False,
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    warning = "review unavailable and no configured deterministic verifier; observe-only (no gate)"
    check(
        not decision.role_spawnable and not decision.role_available and decision.execution == (),
        "missing review and verifier makes medium implementation observe-only",
    )
    check(warning in decision.warnings, "review degradation emits the no-gate warning")


def test_review_unavailable_with_verifier_keeps_pipeline() -> None:
    state = RuntimeState()
    state.set_available("review-hard", False, "down")
    decision = route_prompt(
        "route=implement: рефактор " + " ".join(["кода"] * 40),
        spec=load_intents(),
        mode="dynamic",
        state=state,
        persist=False,
        workspace_root=str(REPO_ROOT),
        registry=load_registry(REPO_CONFIG),
    )
    verify_stages = [stage for stage in decision.execution if stage.kind == "verify"]
    check(
        decision.role_spawnable
        and len(verify_stages) == 2
        and not any(stage.role == "review-hard" for stage in decision.execution),
        "configured verifier keeps the medium implementation pipeline",
    )
    check(
        "review unavailable; degraded to deterministic verify only" in decision.warnings,
        "deterministic-only degradation emits the configured warning",
    )


def test_high_risk_review_availability_composition() -> None:
    independent_only = compose_execution(
        "implement",
        "high",
        "high",
        "implement-hard",
        "implement-hard",
        review_independent_ok=True,
        review_hard_ok=False,
    )
    review_stages = [
        stage
        for stage in independent_only
        if stage.role in {"review-panel", "review-hard", "review-independent"}
    ]
    check(
        [stage.role for stage in review_stages] == ["review-independent"],
        "high-risk pipeline uses single independent review when internal review is unavailable",
    )

    state = RuntimeState()
    state.set_available("review-independent", True, "grok login verified")
    state.set_available("review-hard", False, "down")
    independent_route = route_prompt(
        "route=implement: authentication refactor " + " ".join(["кода"] * 80),
        spec=load_intents(),
        mode="dynamic",
        state=state,
        persist=False,
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    check(
        independent_route.role_spawnable
        and [stage.role for stage in independent_route.execution].count("review-independent") == 1
        and not any(stage.role == "review-panel" for stage in independent_route.execution),
        "single independent review satisfies post-composition review degradation",
    )

    unavailable = RuntimeState()
    unavailable.set_available("review-independent", False, "down")
    unavailable.set_available("review-hard", False, "down")
    no_review_route = route_prompt(
        "route=implement: authentication refactor " + " ".join(["кода"] * 80),
        spec=load_intents(),
        mode="dynamic",
        state=unavailable,
        persist=False,
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    check(
        not no_review_route.role_spawnable and no_review_route.execution == (),
        "high-risk route with neither review nor verifier degrades observe-only",
    )


def test_low_complexity_without_review_stays_gated() -> None:
    state = RuntimeState()
    state.set_available("review-hard", False, "down")
    decision = route_prompt(
        "route=implement: Добавь unit-тест для существующей функции",
        spec=load_intents(),
        mode="dynamic",
        state=state,
        persist=False,
        workspace_root="/tmp/other-repo",
        registry=load_registry(REPO_CONFIG),
    )
    check(
        decision.complexity == "low"
        and decision.risk == "low"
        and decision.role_spawnable
        and decision.would_deny_edits,
        "low-risk low-complexity implementation remains normally gated",
    )
    check(
        not any(
            stage.role in {"review-hard", "review-independent"} for stage in decision.execution
        ),
        "low-risk low-complexity implementation has no review stage",
    )


def test_review_hard_alias_public() -> None:
    reg = load_registry(REPO_CONFIG)
    check(reg.canonical("review") == "review-hard", "public review alias -> review-hard")
    # A spawn of type review-hard is model-independent from a Terra implementation.
    stages = (
        ExecutionStage("implement-standard", True, "implementation", ()),
        ExecutionStage("review-hard", True, "internal review", ()),
    )
    reg2 = RoleRegistry(
        {
            "implement-standard": Role(
                name="implement-standard", model="gpt-5.6-terra", capability_mode="all", write=True
            ),
            "review-hard": Role(
                name="review-hard",
                model="gpt-5.6-sol",
                capability_mode="read-only",
                write=False,
                review=True,
            ),
        },
        known_models=frozenset({"gpt-5.6-terra", "gpt-5.6-sol"}),
    )
    valid, warnings = validate_execution(stages, reg2)
    check(valid, f"review-hard internal review is valid and independent ({warnings})")


def test_cheap_fallback_conditions() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.set_available("implement-cheap", False, "down")
    feats = extract_features("напиши тест для demo-api", load_intents())
    # No known verifier -> escalate to Terra, never the writable Flash fallback.
    role, reasons, degraded = select_implement_role(
        "low", "low", feats, state, registry, None, verifier_known=False
    )
    check(role == "implement-standard", f"no verifier escalates to Terra ({role})")
    # Known verifier + all primary/overflow tiers down -> writable Flash fallback.
    state.set_available("implement-standard", False, "down")
    state.set_available("implement-strong", False, "down")
    state.set_available("implement-hard", False, "down")
    state.set_available("implement-overflow", False, "down")
    role, reasons, degraded = select_implement_role(
        "low", "low", feats, state, registry, None, verifier_known=True
    )
    check(role == "implement-cheap-fallback" and degraded, f"writable Flash fallback ({role})")
    # High-risk work never uses the writable Flash fallback.
    for r in (
        "implement-cheap",
        "implement-standard",
        "implement-strong",
        "implement-hard",
        "implement-ops",
        "implement-overflow",
    ):
        state.set_available(r, False, "down")
    feats_risk = extract_features("прошей роутер на x86", load_intents())
    role, reasons, degraded = select_implement_role(
        "low", "high", feats_risk, state, registry, None, verifier_known=True
    )
    check(role is None, f"writable Flash fallback never for high risk ({role})")


def test_review_independent_with_signal() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)
    registry = load_registry(REPO_CONFIG)
    unavailable = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        workspace_root=ws,
        state=RuntimeState(),
        registry=registry,
        persist=False,
    )
    check(
        unavailable.reason == "security_verifier_unavailable"
        and unavailable.write_policy == "observe"
        and unavailable.execution == (),
        "unavailable security verifier makes the whole route observe-only with a distinct reason",
    )
    check(
        any("security verifier unavailable" in warning for warning in unavailable.warnings),
        "unavailable security verifier emits a clear warning",
    )

    state = RuntimeState()
    state.set_available("review-independent", True, "grok login verified (test)")
    state.set_available("security-verify", True, "grok login verified (test)")
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        workspace_root=ws,
        state=state,
        registry=registry,
        persist=False,
    )
    check(
        any(s.role == "review-independent" for s in d.execution),
        "verified signal composes independent review for high-risk security",
    )
    check(
        any(s.role == "security-verify" for s in d.execution) and d.would_deny_edits,
        "available security verifier composes an enforceable verification stage",
    )


def test_verifier_workspace_awareness() -> None:
    spec = pipeline.apply_config_verifiers(load_intents())
    ws = str(REPO_ROOT)
    check(
        resolve_verifier(ws, spec)
        == ("./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"),
        "dotfiles fixture resolves its verifier",
    )
    check(resolve_verifier("/tmp/other-repo", spec) is None, "generic workspace has no verifier")
    check(resolve_verifier(None, spec) is None, "unknown workspace has no verifier")
    scalar_spec = {"verifiers": {str(REPO_ROOT): "./tests/grok-route-test.sh"}}
    check(
        resolve_verifier(ws, scalar_spec) == ("./tests/grok-route-test.sh",),
        "scalar verifier remains a single-stage tuple",
    )
    foreign_dotfiles = Path(tempfile.mkdtemp(prefix="dotfiles-")) / "dotfiles"
    foreign_dotfiles.mkdir()
    check(
        resolve_verifier(str(foreign_dotfiles), spec) is None,
        "foreign workspace named dotfiles does not inherit the verifier",
    )
    d = route_prompt(
        "прошей роутер на x86", spec=spec, mode="static", workspace_root="/tmp/other-repo"
    )
    check(
        not any(s.kind == "verify" for s in d.execution),
        "generic workspace does not block on the dotfiles verifier",
    )


def test_verifier_config_resolution() -> None:
    previous_grok_home = os.environ.get("GROK_HOME")
    try:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            grok_home = base / "grok-a"
            workspace = base / "workspace-b"
            grok_home.mkdir()
            workspace.mkdir()
            stub = workspace / "verify.sh"
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
            (grok_home / "config.toml").write_text(
                f'[routing.verifiers]\n"{workspace}" = ["./verify.sh"]\n',
                encoding="utf-8",
            )
            os.environ["GROK_HOME"] = str(grok_home)
            config_map = pipeline.load_verifier_map()
            merged = pipeline.apply_config_verifiers({})
            check(bool(config_map), "effective config loads a non-empty verifier map")
            check(
                resolve_verifier(str(workspace), merged) == ("./verify.sh",),
                "effective config verifier resolves for an absolute workspace key",
            )

            empty_home = base / "grok-c"
            empty_home.mkdir()
            (empty_home / "config.toml").write_text("[privacy]\nenabled = true\n", encoding="utf-8")
            os.environ["GROK_HOME"] = str(empty_home)
            inline = {"verifiers": {str(workspace): ["./verify.sh"]}}
            check(
                pipeline.load_verifier_map() == {},
                "config without verifier table loads an empty map",
            )
            check(
                pipeline.apply_config_verifiers(inline) is inline,
                "absent config verifier table leaves the inline spec unchanged",
            )
            check(
                resolve_verifier(str(workspace), inline) == ("./verify.sh",),
                "inline verifier remains resolvable when config has no verifier table",
            )
    finally:
        if previous_grok_home is None:
            os.environ.pop("GROK_HOME", None)
        else:
            os.environ["GROK_HOME"] = previous_grok_home


def test_verifier_command_exact() -> None:
    expected = "./tests/grok-route-test.sh"
    second = "./tests/grok-combine-install-test.sh"
    check(
        is_verifier_command("./tests/grok-route-test.sh", expected),
        "exact configured argv accepted",
    )
    check(is_verifier_command(second, second), "exact second verifier argv accepted")
    check(
        not is_verifier_command("./tests/grok-route-test.sh", second),
        "first verifier does not satisfy second",
    )
    check(
        not is_verifier_command("bash -c 'echo pwn' ./tests/grok-route-test.sh", expected),
        "interpreter wrapper denied",
    )
    check(
        not is_verifier_command("sh ./tests/grok-route-test.sh", expected),
        "alternate interpreter denied",
    )
    check(
        not is_verifier_command("./tests/grok-route-test.sh --extra", expected), "extra args denied"
    )
    check(
        not is_verifier_command("./tests/grok-route-test.sh; rm -rf /", expected),
        "metacharacter denied",
    )
    check(not is_verifier_command("", expected), "empty command denied")


def test_availability_signal_ordering() -> None:
    reg = load_registry(REPO_CONFIG)
    state = RuntimeState()
    check(
        not review_independent_available(state, reg, stale=False),
        "no signal -> grok review unavailable",
    )
    state.set_available("review-independent", True, "grok login verified")
    check(review_independent_available(state, reg, stale=False), "explicit signal -> available")
    state.record_completion("review-independent", True)
    check(
        review_independent_available(state, reg, stale=False),
        "spawn success does not erase operator signal",
    )
    check(
        state.status_for("review-independent").availability_signal == "grok login verified",
        "operator provenance retained separately",
    )
    check(
        not review_independent_available(state, reg, stale=True),
        "stale state never enables grok review",
    )
    state.set_available("review-independent", False, "down")
    check(
        not review_independent_available(state, reg, stale=False),
        "negative signal clears provenance",
    )


def test_short_ambiguous_escalates_to_terra() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.set_available("implement-cheap", False, "down")
    for prompt in ("напиши патч для demo-api", "почини это"):
        feats = extract_features(prompt, load_intents())
        role, reasons, degraded = select_implement_role(
            "low", "low", feats, state, registry, None, verifier_known=True
        )
        check(
            role == "implement-standard" and degraded,
            f"short ambiguous '{prompt}' escalates to Terra ({role})",
        )


def test_short_testable_still_flash_fallback() -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.set_available("implement-cheap", False, "down")
    state.set_available("implement-standard", False, "down")
    state.set_available("implement-strong", False, "down")
    state.set_available("implement-hard", False, "down")
    state.set_available("implement-overflow", False, "down")
    feats = extract_features("напиши тест", load_intents())
    role, reasons, degraded = select_implement_role(
        "low", "low", feats, state, registry, None, verifier_known=True
    )
    check(
        role == "implement-cheap-fallback" and degraded,
        f"short testable prompt still uses Flash fallback ({role})",
    )


def test_unit_test_cheap_luna() -> None:
    spec = load_intents()
    d = route_prompt(
        "Добавь unit-тест для существующей функции",
        spec=spec,
        mode="dynamic",
        state=RuntimeState(),
        persist=False,
        registry=load_registry(REPO_CONFIG),
    )
    check(
        d.intent == "implement" and d.role == "implement-cheap",
        f"unit test -> cheap Luna ({d.role})",
    )
    check(
        d.complexity == "low" and d.risk == "low",
        f"unit test is low complexity/risk ({d.complexity}/{d.risk})",
    )


def test_direct_review_pipeline() -> None:
    spec = load_intents()
    stages = compose_execution(
        "review",
        "low",
        "high",
        "review-hard",
        None,
        review_independent_ok=True,
        review_hard_ok=True,
    )
    check(
        [s.role for s in stages] == ["review-hard"] and stages[0].required,
        "direct review composes only required review-hard",
    )
    reg = load_registry(REPO_CONFIG)
    valid, warnings = validate_execution(stages, reg)
    check(
        valid and reg.get("review-hard").write is False and reg.get("review-hard").review is True,
        f"direct review is read-only and review-capable ({warnings})",
    )

    state = RuntimeState()
    state.set_available("review-independent", True, "grok login verified")
    routed = route_prompt("review the diff", spec=spec, mode="dynamic", state=state, persist=False)
    check(
        [s.role for s in routed.execution] == ["review-hard"],
        "grok availability never adds review-independent to direct review",
    )
    check(
        not any("independent review" in w for w in routed.warnings),
        "direct review needs no grok availability signal or warning",
    )
    check(
        {"review", "plan", "explore"}.issubset(gated_intents(spec)),
        "review/plan/explore are policy-gated",
    )
    phrase = route_prompt(
        "напиши патч для demo-api", spec=spec, mode="dynamic", state=RuntimeState(), persist=False
    )
    check(phrase.would_deny_edits and phrase.would_block_stop, "phrase-only implement is gated")
    security_phrase = route_prompt(
        "hardening", spec=spec, mode="dynamic", state=RuntimeState(), persist=False
    )
    check(
        security_phrase.intent == "security"
        and not security_phrase.has_strong
        and not security_phrase.would_deny_edits
        and not security_phrase.would_block_stop,
        "phrase-only security remains observe-only",
    )


def test_repo_wide_high_risk_pipeline() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)
    d = route_prompt(
        "Сделай repo-wide refactor публичного API с миграцией",
        spec=spec,
        mode="static",
        workspace_root=ws,
        registry=load_registry(REPO_CONFIG),
    )
    check(
        d.intent == "implement" and d.complexity == "high" and d.risk == "high",
        "repo-wide is high complexity/risk",
    )
    check(d.role == "implement-hard", "repo-wide is not Luna")
    check(
        [s.role for s in d.execution]
        == ["recon", "plan-hard", "implement-hard", "review-hard", "verify", "verify"],
        f"repo-wide degrades to review-hard ({[s.role for s in d.execution]})",
    )
    recon = d.execution[0]
    check(
        [m.role for m in recon.members]
        == ["explore", "explore", "explore-thorough", "explore", "explore"],
        "high complexity adds architecture/dependencies recon member",
    )

    signalled = compose_execution(
        "implement",
        "high",
        "high",
        "implement-hard",
        "implement-hard",
        review_independent_ok=True,
        review_hard_ok=True,
    )
    review = [stage for stage in signalled if stage.role == "review-panel"]
    check(
        len(review) == 1,
        "high-risk review panel excludes implementer families",
    )
    low = compose_execution("implement", "low", "low", "implement-cheap", "implement-cheap")
    check(
        [stage.role for stage in low] == ["explore", "implement-cheap"]
        and low[0].required
        and low[0].kind == "spawn",
        "low implementation starts with one linear explore, not a barrier",
    )


def test_research_barrier() -> None:
    spec = load_intents()
    registry = load_registry(REPO_CONFIG)
    for prompt in ("deep research on caching", "сравни подходы к кешированию"):
        decision = route_prompt(prompt, spec=spec, mode="static", registry=registry)
        check(
            decision.intent == "research" and decision.role == "researcher",
            f"research route: {prompt}",
        )
    low = compose_execution("research", "low", "low", "researcher", None)
    high = compose_execution(
        "research",
        "high",
        "low",
        "researcher",
        None,
        researcher_challenger_ok=True,
    )
    check(
        [m.role for m in low[0].members] == ["researcher", "researcher-analyst", "researcher"],
        "research low barrier has three members",
    )
    check(
        [m.role for m in high[0].members]
        == [
            "researcher",
            "researcher-analyst",
            "researcher",
            "researcher-challenger",
            "researcher-analyst",
        ],
        "research high barrier adds challenger",
    )
    check(
        registry.get("researcher-challenger").require_availability_signal
        and registry.get("researcher-challenger").write is False,
        "research challenger is signalled and read-only",
    )
    track = type("Track", (), {"incomplete_required_members": lambda self, stage: stage.members})()
    with mock.patch.object(
        gate,
        "_role_availability_verified",
        side_effect=lambda role: role != "researcher-challenger",
    ):
        enforceable = gate._enforceable_barrier_members(track, high[0])
    check(
        [member.role for member in enforceable]
        == [
            "researcher",
            "researcher-analyst",
            "researcher",
            "researcher-challenger",
            "researcher-analyst",
        ],
        "signal-absent challenger remains a required barrier member",
    )


def test_barrier_width_profiles_and_lens_fallback() -> None:
    medium = compose_execution(
        "implement", "medium", "low", "implement-standard", "implement-standard"
    )[0]
    high = compose_execution(
        "implement", "high", "low", "implement-standard", "implement-standard"
    )[0]
    no_signal = compose_execution("research", "high", "low", "researcher", None)[0]
    signalled = compose_execution(
        "research", "high", "low", "researcher", None, researcher_challenger_ok=True
    )[0]
    override = compose_execution(
        "implement",
        "medium",
        "low",
        "implement-standard",
        "implement-standard",
        profile=Profile(name="narrow", recon_barrier_width=2),
    )[0]
    warnings: list[str] = []
    compose_execution(
        "implement",
        "high",
        "low",
        "implement-standard",
        "implement-standard",
        profile=Profile(name="wide", recon_barrier_width=99),
        warnings=warnings,
    )
    check(len(medium.members) == 3, "default medium recon width is three")
    check(len(high.members) == 5, "default high recon width is five")
    check(len(no_signal.members) == 4, "high research skips unverified challenger")
    check(
        len(signalled.members) == 5 and signalled.members[3].role == "researcher-challenger",
        "high research includes signalled challenger",
    )
    check(len(override.members) == 2, "profile override restores the old recon width")
    check(warnings and "clamped" in warnings[0], "oversized barrier width warns when clamped")
    check(
        not load_barrier_lenses("/no/such/barrier-lenses.json"),
        "missing lens file falls back safely",
    )


def test_research_challenger_unavailable_has_no_cross_turn_debt() -> None:
    prompt = (
        "route=research: Conduct deep research comparing distributed caching architectures, "
        "consistency models, cache invalidation strategies, failure recovery, observability, "
        "benchmarking methodology, security tradeoffs, operational costs, deployment patterns, "
        "and alternatives. Analyze evidence from multiple perspectives, investigate current "
        "approaches, compare strengths and weaknesses, synthesize a detailed recommendation, "
        "state assumptions and limitations, identify unresolved questions, and explain how "
        "the findings apply across services, teams, environments, and long-lived production "
        "systems. Include terminology, evaluation criteria, tradeoffs, risks, and follow-up "
        "research directions without proposing code changes."
    )
    spec = hook_route.apply_config_verifiers(load_intents())

    def run_case(signal: bool, session_id: str) -> tuple[dict, RuntimeState, str]:
        with tempfile.TemporaryDirectory(prefix="research-challenger-") as raw_tmp:
            tmp = Path(raw_tmp)
            os.environ["GROK_HOME"] = str(tmp / "grok")
            os.environ["XDG_STATE_HOME"] = str(tmp / "state")
            os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
            os.environ.pop("GROK_ROUTE_MODE", None)
            os.environ.pop("GROK_ROUTE_ENFORCE", None)
            os.environ.pop("GROK_ROUTE_SOFT", None)
            state = RuntimeState(source_path=default_state_path())
            if signal:
                state.set_available("researcher-challenger", True, "verified test signal")
                save_state(state)
            hook_route.handle_prompt(
                {
                    "hookEventName": "UserPromptSubmit",
                    "sessionId": session_id,
                    "prompt": prompt,
                    "cwd": str(REPO_ROOT),
                    "workspaceRoot": str(REPO_ROOT),
                },
                spec,
            )
            route = hook_route.resolve_route(
                session_id,
                spec,
                hook_prompt=prompt,
                persist=False,
                event="user_prompt_submit",
                workspace_root=str(REPO_ROOT),
            )
            loaded = load_state(default_state_path())
            track = max(loaded.executions.values(), key=lambda item: item.turn_id or 0)
            decision_id = track.decision_id
            check(
                route["intent"] == "research" and route["complexity"] == "high",
                f"production UserPromptSubmit composes high-complexity research (signal={signal})",
            )
            assert track is not None
            barrier = next(stage for stage in track.stages if stage.stage_id == "research")
            expected = ["researcher", "researcher-analyst", "researcher"]
            if signal:
                expected.extend(["researcher-challenger", "researcher-analyst"])
            else:
                expected.append("researcher-analyst")
            check(
                [member.role for member in barrier.members] == expected,
                f"production route persists expected research members (signal={signal})",
            )
            if not signal:
                for member in barrier.members:
                    key = track.member_key(barrier.stage_id, member.member_id)
                    record_stage_tx(default_state_path(), decision_id, "result", key, True)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    hook_route.handle_stop(
                        {
                            "hookEventName": "Stop",
                            "sessionId": session_id,
                            "reason": "end_turn",
                            "stopHookActive": True,
                            "cwd": str(REPO_ROOT),
                            "workspaceRoot": str(REPO_ROOT),
                        },
                        spec,
                    )
                final_state = load_state(default_state_path())
                final_track = final_state.get_execution(decision_id)
                assert final_track is not None
                check(
                    final_track.all_required_completed()
                    and final_state.latest_session_debt(session_id, 86400.0) is None
                    and '"decision": "block"' not in output.getvalue(),
                    "production end_turn has no session_debt after normal member completion",
                )
            return route, loaded, str(tmp)

    run_case(False, "research-session-unavailable")
    run_case(True, "research-session-available")


def test_family_fallback_covers_uncatalogued_pool() -> None:
    """Private config pins and gateway aliases must resolve to line families.

    The packaged catalog only describes public models, so the live pool
    (gpt-5.6-*, glm-5.3-std, gemini, mimo) relies on the fallback; without it
    sol+luna count as independent and an ``@commandcode`` alias counts as a
    different family from its own base model.
    """
    from grokbuild.roles import family_of, load_registry

    catalog = load_registry().provider_catalog
    fam = lambda model: family_of(model, catalog)  # noqa: E731
    check(
        fam("gpt-5.6-sol") == fam("gpt-5.6-luna") == fam("gpt-5.6-terra") == "gpt-5.6",
        "the gpt-5.6 line is one family",
    )
    check(
        fam("qwen3.8-max@commandcode") == fam("qwen3.8-max"),
        "a gateway alias belongs to its base model family",
    )
    check(
        fam("deepseek-v4-pro@commandcode") == fam("deepseek-v4-pro"),
        "the deepseek alias matches its base family",
    )
    check(fam("glm-5.3") == fam("glm-5.3-flash"), "glm std and flash share the glm-5.3 family")
    check(fam("gemini-3.7-flash") == "gemini-3.7", "gemini flash folds to its line")
    check(fam("grok-4.6") != fam("gpt-5.6-sol"), "distinct vendors stay distinct")
    check(
        fam("kimi-k3") == "kimi-k3" and fam("minimax-m3") == "minimax-m3",
        "digit-bearing suffixes are model names, not line variants",
    )
    check(
        fam("qwen3.8-max") == "qwen3.8",
        "catalog-declared families always win over the fallback",
    )


def main() -> int:
    test_verifier_metachar_controls_rejected()
    test_conductor_handoff_roundtrip()
    test_conductor_resolution()
    test_second_opinion_matrix()
    test_tier_selection()
    test_failure_escalation()
    test_degraded_fallback()
    test_overflow_after_primary_tiers()
    test_flash_fallback_only_low_risk()
    test_aliases()
    test_conductor_flash_catalog_has_only_the_criterion_role_pin()
    test_grok_roles_read_only_no_binding()
    test_read_only_registry_validation()
    test_review_independence_validation()
    test_review_independence_uses_model_family()
    test_unpinned_independent_review_warns_but_allows()
    test_internal_review_alternatives_are_inert()
    test_internal_review_same_family_marks_degraded_but_allows()
    test_internal_review_different_family_is_clean()
    test_degraded_review_marker_reaches_route_telemetry()
    test_pipeline_compat()
    test_review_hard_internal_review()
    test_hard_tier_medium_risk_gets_review()
    test_overflow_medium_complexity_gets_review()
    test_review_unavailable_without_verifier_observe_only()
    test_review_unavailable_with_verifier_keeps_pipeline()
    test_high_risk_review_availability_composition()
    test_low_complexity_without_review_stays_gated()
    test_review_hard_alias_public()
    test_cheap_fallback_conditions()
    test_review_independent_with_signal()
    test_verifier_workspace_awareness()
    test_verifier_config_resolution()
    test_verifier_command_exact()
    test_availability_signal_ordering()
    test_short_ambiguous_escalates_to_terra()
    test_short_testable_still_flash_fallback()
    test_unit_test_cheap_luna()
    test_direct_review_pipeline()
    test_repo_wide_high_risk_pipeline()
    test_research_barrier()
    test_barrier_width_profiles_and_lens_fallback()
    test_research_challenger_unavailable_has_no_cross_turn_debt()
    test_family_fallback_covers_uncatalogued_pool()
    test_no_intent_policy_shape()
    test_destructive_syntax_and_data_noun_regression()
    test_confirmation_batch_kind_is_spawnable()
    test_confirmation_batch_profile_serialization_and_inheritance()
    test_attributed_success_composes_confirmation_after_first_verify()
    test_default_confirmation_composition_has_structural_parity()

    with tempfile.TemporaryDirectory() as raw:
        test_verify_stage_tracked_separately(Path(raw))

    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("pipeline tier tests passed")
    return 0


if __name__ == "__main__":
    run_standalone(main)
