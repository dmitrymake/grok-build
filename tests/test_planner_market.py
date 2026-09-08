#!/usr/bin/env python3
"""The planner market: provider-diverse decompositions behind a profile.

A single model's self-reported confidence is a poor uncertainty signal.
Disagreement between independently produced decompositions is a much better
one - but only if the planners are genuinely independent, and only if the
market never runs on tasks cheap enough not to need it. These tests pin both
halves: the market is inert on every default route, and when a profile turns it
on it composes as one barrier plus a signal stage over the same ExecutionTrack.
"""

from __future__ import annotations

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import json  # noqa: E402
import tomllib  # noqa: E402

import grokbuild.roles as roles  # noqa: E402
from grokbuild.compose import compose_execution, load_barrier_lenses  # noqa: E402
from grokbuild.policy import Profile, load_profiles  # noqa: E402
from grokbuild.state import _canonical_stage_slot

MARKET_ROLES = ("planner-a", "planner-b", "planner-c")
FAILURES: list[str] = []


check = make_check(FAILURES)


def _compose(intent: str, complexity: str, profile: Profile | None) -> tuple:
    return compose_execution(
        intent,
        complexity,
        "medium",
        None,
        "implement-standard",
        (),
        True,
        True,
        True,
        profile,
        [],
        False,
    )


def _market_profile() -> Profile:
    return load_profiles()["planner-market"]


def test_default_composition_is_untouched() -> None:
    """No default route may gain a stage from a feature that is switched off."""
    default = load_profiles()["default"]
    check(default.planner_market is None, "the default profile does not enable the market")
    check(
        "planner_market" not in default.to_dict(),
        "an unset flag is absent from the serialized profile",
    )
    for intent in ("implement", "plan", "review", "explore", "security", "research"):
        for complexity in ("low", "medium", "high"):
            stages = _compose(intent, complexity, default)
            roles_seen = {stage.role for stage in stages}
            check(
                not (roles_seen & {*MARKET_ROLES, "plan-comparator", "planner-market"}),
                f"{intent}/{complexity} composes no market stage ({sorted(roles_seen)})",
            )


def test_market_composes_one_barrier_and_a_signal_stage() -> None:
    profile = _market_profile()
    stages = _compose("implement", "medium", profile)
    barriers = [stage for stage in stages if stage.stage_id == "planner-market"]
    check(len(barriers) == 1, f"exactly one market barrier ({len(barriers)})")
    barrier = barriers[0]
    check(barrier.kind == "parallel_spawn", f"the market is a barrier ({barrier.kind})")
    check(barrier.required, "the market barrier is required once enabled")
    check(
        tuple(member.role for member in barrier.members) == MARKET_ROLES,
        f"three independent planners ({[m.role for m in barrier.members]})",
    )
    check(
        len({member.member_id for member in barrier.members}) == 3,
        "members carry distinct ids",
    )
    comparator = [stage for stage in stages if stage.role == "plan-comparator"]
    check(len(comparator) == 1, f"exactly one comparator stage ({len(comparator)})")
    check(
        comparator[0].kind == "spawn" and "never" not in comparator[0].reason.lower(),
        "the comparator is an ordinary linear stage",
    )
    check(
        "no selection authority" in comparator[0].reason,
        f"the comparator's reason states it does not select ({comparator[0].reason})",
    )


def test_market_only_runs_on_uncertain_tasks() -> None:
    """N planners cost N spawns; a trivial task must not pay for them."""
    profile = _market_profile()
    low = {stage.role for stage in _compose("implement", "low", profile)}
    check("planner-market" not in low, f"low complexity skips the market ({sorted(low)})")
    for complexity in ("medium", "high"):
        stages = {stage.stage_id for stage in _compose("implement", complexity, profile)}
        check("planner-market" in stages, f"{complexity} implement runs the market")
        planning = {stage.stage_id for stage in _compose("plan", complexity, profile)}
        check("planner-market" in planning, f"{complexity} plan runs the market")
    review = {stage.stage_id for stage in _compose("review", "high", profile)}
    check("planner-market" not in review, "a review route has nothing to decompose")


def test_market_stages_keep_distinct_slots() -> None:
    """Slot collisions silently drop recorded progress during reconciliation."""
    profile = _market_profile()
    stages = _compose("implement", "high", profile)
    slots = [stage.slot for stage in stages]
    check(len(slots) == len(set(slots)), f"every composed stage has its own slot ({slots})")
    check("planner-market" in slots, "the market barrier owns a named slot")
    check("plan-comparator" in slots, "the comparator owns a named slot")
    check(
        "plan" in slots,
        "the ordinary plan stage keeps its own slot alongside the market",
    )


def test_slot_mapping_is_explicit_for_market_roles() -> None:
    from grokbuild.decision import ExecutionStage

    for role in MARKET_ROLES:
        check(
            _canonical_stage_slot(ExecutionStage(role, True, "x"), 0) == "planner-market",
            f"{role} maps to the market slot",
        )
    check(
        _canonical_stage_slot(ExecutionStage("plan-comparator", True, "x"), 0) == "plan-comparator",
        "the comparator maps to its own slot",
    )
    check(
        _canonical_stage_slot(ExecutionStage("plan-hard", True, "x"), 0) == "plan",
        "ordinary planning is unaffected",
    )


def test_planners_are_provider_diverse() -> None:
    """Two endpoints of one model family are redundancy, not diversity."""
    config = tomllib.loads((REPO_ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
    bindings = config["subagents"]["roles"]
    models = {role: bindings[role]["model"] for role in MARKET_ROLES}
    check(len(set(models.values())) == 3, f"three distinct model pins ({models})")
    providers = json.loads((REPO_ROOT / "grokbuild" / "providers.json").read_text())["providers"]
    provider_of = {
        model: provider for provider, spec in providers.items() for model in spec.get("models", {})
    }
    seen = {role: provider_of.get(model) for role, model in models.items()}
    check(len(set(seen.values())) == 3, f"three distinct providers ({seen})")


def test_market_roles_are_read_only_everywhere() -> None:
    """A planner that could write would be an executor, not a planner."""
    for role in (*MARKET_ROLES, "plan-comparator"):
        check(role in roles.READ_ONLY_CANONICAL, f"{role} is canonically read-only")
        check(role in roles.EXECUTION_READ_ONLY_ROLES, f"{role} is edit-gated as read-only")


def test_both_config_surfaces_declare_the_market() -> None:
    surfaces = {}
    for name in ("config.toml", "config.example.toml"):
        data = tomllib.loads((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))
        surfaces[name] = set(data["subagents"]["roles"])
    check(
        surfaces["config.toml"] == surfaces["config.example.toml"],
        f"both surfaces declare the same roles ({surfaces['config.toml'] ^ surfaces['config.example.toml']})",
    )
    for role in (*MARKET_ROLES, "plan-comparator"):
        check(role in surfaces["config.toml"], f"{role} is bound in config")
    for name in surfaces:
        data = tomllib.loads((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))
        criterion_judge = data["subagents"]["roles"].get("criterion-judge", {})
        check(
            criterion_judge.get("model") == "glm-5.3-flash",
            f"criterion-judge is flash-bound on {name} ({criterion_judge})",
        )


def test_market_lens_pool_is_loadable() -> None:
    pools = load_barrier_lenses()
    check("planner-market" in pools, f"the market pool loads ({sorted(pools)})")
    check(
        tuple(role for role, _reason in pools["planner-market"]) == MARKET_ROLES,
        "the pool names the three planners in order",
    )


def test_market_degrades_by_omission_when_the_pool_is_too_small() -> None:
    """A market of one planner measures nothing, so it is skipped, not faked."""
    import grokbuild.compose as pipeline

    original = pipeline._FALLBACK_BARRIER_LENSES["planner-market"]
    loader = pipeline.load_barrier_lenses
    pipeline._FALLBACK_BARRIER_LENSES["planner-market"] = (("planner-a", "only lens"),)
    pipeline.load_barrier_lenses = lambda *args, **kwargs: {}
    try:
        warnings: list[str] = []
        stages = compose_execution(
            "implement",
            "medium",
            "medium",
            None,
            "implement-standard",
            (),
            True,
            True,
            True,
            _market_profile(),
            warnings,
            False,
        )
        check(
            not any(stage.stage_id == "planner-market" for stage in stages),
            "a one-planner market is skipped",
        )
        check(
            any("planner market" in warning for warning in warnings),
            f"the omission is reported ({warnings})",
        )
    finally:
        pipeline._FALLBACK_BARRIER_LENSES["planner-market"] = original
        pipeline.load_barrier_lenses = loader
