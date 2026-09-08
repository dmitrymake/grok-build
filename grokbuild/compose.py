#!/usr/bin/env python3
"""Composition of execution stages for the Grok Build routing pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from grokbuild import verifier_planner
from grokbuild.decision import ExecutionMember, ExecutionStage
from grokbuild.state import _canonical_stage_slot
from grokbuild.policy import Profile
from grokbuild.roles import RoleRegistry, family_of, load_registry
import grokbuild.roles as roles

READ_ONLY_STAGES = roles.EXECUTION_READ_ONLY_ROLES
IMPLEMENT_STAGES = roles.IMPLEMENT_ROLE_NAMES
SECURITY_STAGES = frozenset({"security", "security-verify"})
REVIEW_STAGES = frozenset({"review", "review-hard", "review-independent", "expert-rescue"})
INDEPENDENT_REVIEW_ROLES = frozenset({"review-independent", "expert-rescue"})
INTERNAL_REVIEW_ROLES = frozenset({"review", "review-hard"})
DEGRADED_REVIEW_REASON_CODE = "degraded_review_same_family"

_BARRIER_LENSES_PATH = Path(__file__).resolve().parent / "barrier_lenses.json"
_FALLBACK_BARRIER_LENSES = {
    "recon": (
        ("explore", "structure/map lens"),
        ("explore", "callers/tests lens"),
        ("explore-thorough", "architecture/dependencies lens"),
    ),
    "recon-advisor": (("explore-risk", "bounded risk flags with one-minute verification checks"),),
    "review": (
        ("review-hard", "correctness/regression lens"),
        ("review-independent", "adversarial/abuse lens"),
        ("expert-rescue", "independent diagnosis lens"),
    ),
    "research": (
        ("researcher", "deep synthesis lead"),
        ("researcher-analyst", "alternative-perspective analysis"),
        ("researcher", "prior-art/landscape lens"),
    ),
    "planner-market": (
        ("planner-a", "independent decomposition (primary provider)"),
        ("planner-b", "independent decomposition (redundant provider, same family)"),
        ("planner-c", "independent decomposition (different model family)"),
        ("planner-strong", "optional strong fourth decomposition; not provider-diverse"),
    ),
}


JUDGE_PANEL = (("judge-primary", "compare A then B"), ("judge-independent", "compare B then A"))


def _soft_review_independence(
    stages: tuple[ExecutionStage, ...],
    registry: RoleRegistry,
) -> tuple[tuple[ExecutionStage, ...], list[str]]:
    implement_families = {
        family_of(role.model, registry.provider_catalog)
        for stage in stages
        for name in (
            tuple(member.role for member in stage.members)
            if stage.kind == "parallel_spawn"
            else (stage.role,)
        )
        if name in IMPLEMENT_STAGES
        if (role := registry.get(name)) is not None and role.model
    }
    implement_families.discard(None)
    independent_review_present = any(
        name in INDEPENDENT_REVIEW_ROLES
        and (role := registry.get(name)) is not None
        and role.model
        and family_of(role.model, registry.provider_catalog) not in implement_families
        for stage in stages
        for name in (
            tuple(member.role for member in stage.members)
            if stage.kind == "parallel_spawn"
            else (stage.role,)
        )
    )
    warnings: list[str] = []
    normalized: list[ExecutionStage] = []
    for stage in stages:
        if stage.kind == "parallel_spawn":
            members = []
            if stage.stage_id == "review-panel":
                seen_families: set[str | None] = set()
                for member in stage.members:
                    role = registry.get(member.role)
                    family = family_of(role.model, registry.provider_catalog) if role else None
                    if family in seen_families:
                        warnings.append(f"review panel members share provider family: {family}")
                    seen_families.add(family)
            for member in stage.members:
                role = registry.get(member.role)
                family = family_of(role.model, registry.provider_catalog) if role else None
                if (
                    not independent_review_present
                    and member.role in INTERNAL_REVIEW_ROLES
                    and family in implement_families
                ):
                    warnings.append(
                        f"degraded-review: same-family internal review '{member.role}' "
                        f"(reason_code={DEGRADED_REVIEW_REASON_CODE})"
                    )
                members.append(member)
            normalized.append(replace(stage, members=tuple(members)))
            continue
        if independent_review_present or stage.role not in INTERNAL_REVIEW_ROLES:
            normalized.append(stage)
            continue
        role = registry.get(stage.role)
        if role is None or not role.model:
            normalized.append(stage)
            continue
        family = family_of(role.model, registry.provider_catalog)
        if family not in implement_families:
            normalized.append(stage)
            continue
        warnings.append(
            f"degraded-review: same-family internal review '{stage.role}' "
            f"(reason_code={DEGRADED_REVIEW_REASON_CODE})"
        )
        normalized.append(stage)
    return tuple(normalized), warnings


def validate_execution(
    stages: tuple[ExecutionStage, ...],
    registry: RoleRegistry,
) -> tuple[bool, list[str]]:
    """Validate every composed stage/fallback against the registry.

    A read-only stage must be a read-only role; an implement stage must be able
    to write; a security stage must be security-capable; a review stage must be
    review-capable. Independent review must be family-distinct from the selected
    implementer; the conductor is not a spawn stage and is irrelevant here.
    Verification stages are non-spawnable and never checked against the role
    registry.
    """

    stages, warnings = _soft_review_independence(stages, registry)
    barrier_ids = [stage.stage_id for stage in stages if stage.kind == "parallel_spawn"]
    if len(barrier_ids) != len(set(barrier_ids)):
        return False, ["parallel stage ids must be unique within a decision"]
    verify_ids = [stage.stage_id for stage in stages if stage.kind == "verify"]
    if len(verify_ids) != len(set(verify_ids)):
        return False, ["verify stage ids must be unique within a decision"]
    judge_roles = {
        "criterion-judge",
        "judge-primary",
        "judge-independent",
        "judge-disagreement",
        "judge-frontier-code",
        "judge-frontier-general",
        "judge-challenger-agentic",
        "judge-challenger-structural",
    }
    seen_verify = False
    for stage in stages:
        if stage.kind == "verify":
            seen_verify = True
        stage_roles = {stage.role, *(member.role for member in stage.members)}
        if (stage.kind == "judge" or stage_roles & judge_roles) and not seen_verify:
            return False, ["semantic judge stages must follow deterministic verification"]
        if stage.kind == "evidence_collection" and stage_roles & judge_roles:
            return False, ["judge roles cannot run evidence collection"]
    spawn_specs: list[tuple[str, tuple[str, ...]]] = []
    for stage in stages:
        if stage.kind == "parallel_spawn":
            spawn_specs.extend((member.role, ()) for member in stage.members)
        elif stage.spawnable:
            spawn_specs.append((stage.role, stage.alternatives))
    stage_models = [
        (name, (registry.get(name).model if registry.get(name) else None))
        for name, _alternatives in spawn_specs
    ]
    for stage in stages:
        if not stage.spawnable:
            if not stage.command:
                warnings.append(f"verification stage '{stage.role}' has no deterministic command")
                return False, warnings
            continue
        specs = (
            [(member.role, ()) for member in stage.members]
            if stage.kind == "parallel_spawn"
            else [(stage.role, stage.alternatives)]
        )
        if stage.kind == "parallel_spawn" and (not stage.stage_id or not stage.members):
            warnings.append(f"parallel stage '{stage.role}' needs a stage id and members")
            return False, warnings
        if stage.kind == "parallel_spawn":
            member_ids = [member.member_id for member in stage.members]
            if any(not member_id for member_id in member_ids) or len(member_ids) != len(
                set(member_ids)
            ):
                warnings.append(
                    f"parallel stage '{stage.role}' member ids must be non-empty and unique"
                )
                return False, warnings
        if stage.kind == "parallel_spawn" and stage.stage_id == "consilium":
            providers = set()
            for member in stage.members:
                role = registry.get(member.role)
                meta = registry.provider_catalog.get(role.model) if role and role.model else None
                provider = (
                    meta.provider if meta is not None else (role.provider if role else "unknown")
                )
                if provider != "unknown":
                    providers.add(provider)
            if len(providers) < 3:
                warnings.append("consilium parallel stage must span at least 3 distinct providers")
                return False, warnings
        for stage_role, alternatives in specs:
            role = registry.get(stage_role)
            if role is None:
                warnings.append(f"execution stage '{stage_role}' is not spawnable")
                return False, warnings
            if stage_role in READ_ONLY_STAGES and role.write is True:
                warnings.append(f"read-only stage '{stage_role}' must be read-only")
                return False, warnings
            if stage_role in IMPLEMENT_STAGES and role.write is False:
                warnings.append(f"implement stage '{stage_role}' must be writable")
                return False, warnings
            if stage_role in SECURITY_STAGES and role.security is False:
                warnings.append(f"security stage '{stage_role}' is not security-capable")
                return False, warnings
            if stage_role in REVIEW_STAGES:
                if role.review is False:
                    warnings.append(f"review stage '{stage_role}' is not review-capable")
                    return False, warnings
                if stage_role in INDEPENDENT_REVIEW_ROLES:
                    for other_role, other_model in stage_models:
                        if other_role not in IMPLEMENT_STAGES:
                            continue
                        if not role.model or not other_model:
                            warnings.append("review independence unverifiable: unpinned model")
                            continue
                        review_family = family_of(role.model, registry.provider_catalog)
                        implement_family = family_of(other_model, registry.provider_catalog)
                        if review_family is not None and review_family == implement_family:
                            warnings.append(
                                f"review stage '{stage_role}' must be family-independent from implementer '{other_role}'"
                            )
                            return False, warnings
            for alt in alternatives:
                if registry.get(alt) is None:
                    warnings.append(
                        f"alternative '{alt}' for stage '{stage_role}' is not spawnable"
                    )
                    return False, warnings
    return True, warnings


def compose_consilium_barrier(
    registry: RoleRegistry,
    provider_catalog: Mapping[str, Any] | None = None,
) -> ExecutionStage | None:
    """Build the runtime consilium barrier only when provider diversity is valid."""
    stage = ExecutionStage(
        role="consilium",
        required=True,
        reason="repeated repair failures require cross-provider diagnosis",
        kind="parallel_spawn",
        stage_id="consilium",
        slot="consilium",
        members=(
            ExecutionMember("0", "consilium-analyst", True, "fresh-eyes telemetry analysis"),
            ExecutionMember("1", "consilium-challenger", True, "adversarial hypothesis cross-exam"),
            ExecutionMember("2", "consilium-arbiter", True, "verdict and repair plan"),
        ),
    )
    if provider_catalog is not None:
        registry = registry.with_provider_catalog(provider_catalog)
    valid, _warnings = validate_execution((stage,), registry)
    return stage if valid else None


def _slot_execution(stages: list[ExecutionStage]) -> tuple[ExecutionStage, ...]:
    counts: dict[str, int] = {}
    result: list[ExecutionStage] = []
    for stage in stages:
        kind_index = counts.get(stage.kind, 0)
        counts[stage.kind] = kind_index + 1
        slot = _canonical_stage_slot(stage, kind_index)
        result.append(replace(stage, slot=slot))
    return tuple(result)


def load_barrier_lenses(path: Path | str | None = None) -> dict[str, tuple[tuple[str, str], ...]]:
    target = Path(path) if path is not None else _BARRIER_LENSES_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return {}
        barriers = data["barriers"]
        if not isinstance(barriers, dict):
            return {}
        result = {}
        for name in ("recon", "recon-advisor", "research", "planner-market"):
            entries = barriers[name]
            if not isinstance(entries, list) or not entries:
                return {}
            parsed = []
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("role"), str)
                    or not isinstance(entry.get("reason"), str)
                ):
                    return {}
                parsed.append((entry["role"], entry["reason"]))
            result[name] = tuple(parsed)
        if isinstance(barriers.get("review"), list) and barriers["review"]:
            result["review"] = tuple(
                (entry["role"], entry["reason"])
                for entry in barriers["review"]
                if isinstance(entry, dict)
                and isinstance(entry.get("role"), str)
                and isinstance(entry.get("reason"), str)
            )
        return result
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _barrier_members(
    barrier: str,
    width: int,
    challenger_ok: bool = False,
    warnings: list[str] | None = None,
) -> tuple[ExecutionMember, ...]:
    pools = load_barrier_lenses() or _FALLBACK_BARRIER_LENSES
    pool = pools.get(barrier, _FALLBACK_BARRIER_LENSES[barrier])
    target = max(1, min(int(width), len(pool)))
    if target != width and warnings is not None:
        warnings.append(f"{barrier}_barrier_width clamped from {width} to {target}")
    selected = []
    for role, reason in pool:
        if barrier == "research" and role == "researcher-challenger" and not challenger_ok:
            continue
        selected.append((role, reason))
        if len(selected) == target:
            break
    return tuple(
        ExecutionMember(str(index), role, True, reason)
        for index, (role, reason) in enumerate(selected)
    )


def _review_reason(role: str) -> str:
    label = "independent" if role in {"review-independent", "expert-rescue"} else "internal"
    return f"{label} review (read-only)"


def _review_panel_members(
    width: int,
    stages: list[ExecutionStage],
    registry: RoleRegistry,
    review_hard_ok: bool,
    review_independent_ok: bool,
    expert_rescue_ok: bool,
    warnings: list[str] | None,
) -> tuple[ExecutionMember, ...]:
    pools = load_barrier_lenses() or _FALLBACK_BARRIER_LENSES
    availability = {
        "review-hard": review_hard_ok,
        "review-independent": review_independent_ok,
        "expert-rescue": expert_rescue_ok,
    }
    implement_families = {
        family_of(role.model, registry.provider_catalog)
        for stage in stages
        for role_name in (
            (stage.role,)
            if stage.kind != "parallel_spawn"
            else tuple(m.role for m in stage.members)
        )
        if role_name in IMPLEMENT_STAGES
        if (role := registry.get(role_name)) is not None and role.model
    }
    selected: list[tuple[str, str]] = []
    families: set[str | None] = set()
    for role_name, reason in pools.get("review", _FALLBACK_BARRIER_LENSES["review"]):
        role = registry.get(role_name)
        if not availability.get(role_name, False) or role is None or not role.model:
            continue
        family = family_of(role.model, registry.provider_catalog)
        if family in families or family in implement_families:
            continue
        selected.append((role_name, reason))
        families.add(family)
        if len(selected) >= max(1, int(width)):
            break
    if len(selected) < width and warnings is not None:
        warnings.append(f"review_barrier_width clamped from {width} to {len(selected)}")
    if len(selected) < 2 and warnings is not None:
        warnings.append("review panel degraded: insufficient family-diverse reviewers")
    return tuple(
        ExecutionMember(str(i), role, True, reason) for i, (role, reason) in enumerate(selected)
    )


def _planner_market_stages(
    profile: Profile | None,
    complexity: str,
    warnings: list[str] | None = None,
) -> list[ExecutionStage]:
    """Compose the planner market, or nothing at all.

    Independent decompositions from provider-diverse planners are a far stronger
    uncertainty signal than one model's self-reported confidence, but N planners
    cost N spawns. The market therefore runs only when a profile asks for it and
    only on tasks uncertain enough to be worth it; every other route composes
    exactly as it did before.

    The comparator reports agreement across the decompositions. It is a signal
    producer, not an arbiter: nothing here selects a plan, and the harness keeps
    that authority.
    """
    if profile is None or not profile.planner_market:
        return []
    if complexity not in {"medium", "high"}:
        return []
    members = _barrier_members("planner-market", profile.planner_market_width, warnings=warnings)
    if len(members) < 2:
        if warnings is not None:
            warnings.append("planner market needs at least two independent planners; skipped")
        return []
    return [
        ExecutionStage(
            "planner-market",
            True,
            "independent decompositions from provider-diverse planners",
            (),
            kind="parallel_spawn",
            stage_id="planner-market",
            members=members,
        ),
        ExecutionStage(
            "plan-comparator",
            True,
            "agreement signal across the competing decompositions (no selection authority)",
            (),
        ),
    ]


def _recon_advisor_member(
    profile: Profile, member_id: int, available: bool, warnings: list[str] | None
) -> ExecutionMember | None:
    role = profile.recon_advisor_role
    if not role or not available:
        return None
    pools = load_barrier_lenses() or _FALLBACK_BARRIER_LENSES
    match = next(
        ((lens_role, reason) for lens_role, reason in pools["recon-advisor"] if lens_role == role),
        None,
    )
    if match is None:
        if warnings is not None:
            warnings.append(
                f"recon advisor lens missing for profile role {role}; degraded by omission"
            )
        return None
    return ExecutionMember(str(member_id), match[0], True, match[1])


def compose_execution(
    intent: str | None,
    complexity: str,
    risk: str,
    selected_role: str | None,
    impl_role: str | None,
    verify_commands: tuple[str, ...] | None = None,
    review_independent_ok: bool = False,
    review_hard_ok: bool = False,
    researcher_challenger_ok: bool = False,
    profile: Profile | None = None,
    warnings: list[str] | None = None,
    explore_risk_ok: bool = False,
    expert_rescue_ok: bool = False,
    registry: RoleRegistry | None = None,
) -> tuple[ExecutionStage, ...]:
    """The actual executor pipeline: spawnable roles + deterministic verify step.

    Normal complex implementation work gets an internal `review-hard`
    (gpt-5.6-sol, read-only) stage after implementation. High-risk work
    gets an independent `review-independent` (host session, read-only) stage only
    when its explicit safe availability signal is present; otherwise the
    pipeline degrades with a warning and no impossible hard gate.
    """

    if intent is None:
        return ()
    registry = registry or load_registry()
    stages: list[ExecutionStage] = []
    if intent == "security":
        stages.append(
            ExecutionStage(
                selected_role or "security", True, "primary security analysis (read-only)", ()
            )
        )
        stages.append(
            ExecutionStage(
                impl_role or "implement-hard",
                True,
                "implement the fixes (required)",
                (),
            )
        )
        if risk == "high" and review_independent_ok:
            stages.append(
                ExecutionStage(
                    "review-independent",
                    True,
                    "independent general review (host session, read-only)",
                    (),
                )
            )
        stages.append(
            ExecutionStage(
                "security-verify",
                True,
                "independent post-implementation security verification (read-only)",
                (),
            )
        )
        for index, command in enumerate(verify_commands or ()):
            stages.append(
                ExecutionStage(
                    "verify",
                    True,
                    "deterministic verification",
                    (),
                    kind="verify",
                    command=command,
                    stage_id=f"verify/{index}",
                )
            )
            if index == 0 and profile is not None and profile.confirmation_batch:
                stages.append(
                    ExecutionStage(
                        "verifier-planner",
                        True,
                        "second verification pass; partially new checks after first attributed "
                        "success; runtime composition requires attribute_evidence == "
                        '"attributed" once wired',
                        (),
                        kind="confirmation_batch",
                        stage_id="confirmation-batch",
                    )
                )
    elif intent == "implement":
        if complexity == "low":
            stages.append(
                ExecutionStage(
                    "explore", True, "cheap read-only reconnaissance before implementation", ()
                )
            )
        else:
            recon_width = (profile or Profile("default")).recon_barrier_width
            if complexity == "high":
                recon_width += 2
            recon_members = _barrier_members("recon", recon_width, warnings=warnings)
            active_profile = profile or Profile("default")
            advisor = _recon_advisor_member(
                active_profile, len(recon_members), explore_risk_ok, warnings
            )
            if advisor is not None:
                recon_members += (advisor,)
            stages.append(
                ExecutionStage(
                    "recon",
                    True,
                    "parallel read-only reconnaissance barrier",
                    (),
                    kind="parallel_spawn",
                    stage_id="recon",
                    members=tuple(recon_members),
                )
            )
        stages.extend(_planner_market_stages(profile, complexity, warnings))
        if complexity == "high":
            stages.append(
                ExecutionStage("plan-hard", False, "optional plan before implementation", ())
            )
        impl_stage_role = selected_role or impl_role or "implement-standard"
        if impl_stage_role == "explore":
            impl_stage_reason = "degraded read-only recon (implementation unavailable)"
        elif impl_stage_role == "implement-cheap-fallback":
            impl_stage_reason = (
                "degraded writable fallback (low-risk testable work with a known verifier)"
            )
        else:
            impl_stage_reason = "implementation (required)"
        stages.append(ExecutionStage(impl_stage_role, True, impl_stage_reason, ()))
        if risk == "high":
            if review_independent_ok and review_hard_ok:
                members = _review_panel_members(
                    2,
                    stages,
                    registry,
                    review_hard_ok,
                    review_independent_ok,
                    expert_rescue_ok,
                    warnings,
                )
                if len(members) >= 2:
                    stages.append(
                        ExecutionStage(
                            "review-panel",
                            True,
                            "parallel high-risk review barrier",
                            (),
                            kind="parallel_spawn",
                            stage_id="review-panel",
                            members=members,
                        )
                    )
                elif len(members) == 1:
                    stages.append(
                        ExecutionStage(members[0].role, True, _review_reason(members[0].role), ())
                    )
                else:
                    stages.append(
                        ExecutionStage("review-hard", True, "internal review (read-only)", ())
                    )
            elif review_independent_ok:
                stages.append(
                    ExecutionStage(
                        "review-independent",
                        True,
                        "independent review — internal review unavailable, degraded",
                        (),
                    )
                )
            elif review_hard_ok:
                stages.append(
                    ExecutionStage(
                        "review-hard",
                        True,
                        "internal review — independent review unavailable, degraded",
                        (),
                    )
                )
        elif complexity in {"medium", "high"} and review_hard_ok:
            width = (profile or Profile("default")).review_barrier_width
            if complexity == "high" and width > 1:
                members = _review_panel_members(
                    width,
                    stages,
                    registry,
                    review_hard_ok,
                    review_independent_ok,
                    expert_rescue_ok,
                    warnings,
                )
                if len(members) >= 2:
                    stages.append(
                        ExecutionStage(
                            "review-panel",
                            True,
                            "parallel review barrier",
                            (),
                            kind="parallel_spawn",
                            stage_id="review-panel",
                            members=members,
                        )
                    )
                elif len(members) == 1:
                    stages.append(
                        ExecutionStage(members[0].role, True, _review_reason(members[0].role), ())
                    )
                else:
                    stages.append(
                        ExecutionStage("review-hard", True, "internal review (read-only)", ())
                    )
            else:
                stages.append(
                    ExecutionStage("review-hard", True, "internal review (read-only)", ())
                )
        for index, command in enumerate(verify_commands or ()):
            stages.append(
                ExecutionStage(
                    "verify",
                    True,
                    "deterministic verification",
                    (),
                    kind="verify",
                    command=command,
                    stage_id=f"verify/{index}",
                )
            )
            if index == 0 and profile is not None and profile.confirmation_batch:
                stages.append(
                    ExecutionStage(
                        "verifier-planner",
                        True,
                        "second verification pass; partially new checks after first attributed "
                        "success; runtime composition requires attribute_evidence == "
                        '"attributed" once wired',
                        (),
                        kind="confirmation_batch",
                        stage_id="confirmation-batch",
                    )
                )
    elif intent == "review":
        stages.append(
            ExecutionStage(
                "review-hard",
                True,
                "internal review (required, read-only)",
            )
        )
    elif intent == "plan":
        stages.extend(_planner_market_stages(profile, complexity, warnings))
        stages.append(
            ExecutionStage(selected_role or "plan-hard", True, "planning (required, read-only)", ())
        )
        if risk == "high" and review_independent_ok:
            stages.append(
                ExecutionStage("review-independent", True, "independent review of the plan", ())
            )
    elif intent == "explore":
        stages.append(
            ExecutionStage(selected_role or "explore", True, "read-only exploration (required)", ())
        )
    elif intent == "research":
        research_width = (profile or Profile("default")).research_barrier_width
        if complexity == "high":
            research_width += 2
        members = _barrier_members(
            "research", research_width, researcher_challenger_ok, warnings=warnings
        )
        stages.append(
            ExecutionStage(
                "research",
                True,
                "parallel read-only research barrier",
                (),
                kind="parallel_spawn",
                stage_id="research",
                members=tuple(members),
            )
        )
    return _slot_execution(stages)


def has_required_execution(stages: tuple[ExecutionStage, ...]) -> bool:
    """Return whether the composed pipeline has a required stage to gate on."""
    return any(stage.required for stage in stages)


# Dormant control-plane composition (judge/frontier/evidence/adjudication) — AWAITING DATASET; not wired to active routes.
def compose_frontier_stage(
    profile: Profile | None, decision, stage_id: str = "frontier-escalation"
) -> list[ExecutionStage]:
    """Compose the rare meta-planning stage, or nothing at all.

    Escalation happens before execution: buying framing, decomposition and
    invariants from an expensive model and handing them to cheap ones is the
    whole economic argument. Escalating after a bad cheap plan has already run a
    swarm has paid for the swarm and polluted the trajectory.

    The stage is read-only by construction - the frontier resolver is a
    meta-planner, never an executor - and it appears only when a profile asks
    for escalation and the observable signals cross the threshold.
    """
    if profile is None or not profile.frontier_escalation:
        return []
    if decision is None or not decision.escalate:
        return []
    top = decision.contributions[0][0] if decision.contributions else "observed signals"
    return [
        ExecutionStage(
            "frontier-resolver",
            True,
            f"frontier meta-planning: {top} at score {decision.score:.2f} "
            f"crossed {decision.threshold:.2f}",
            ("frontier-resolver-standby",),
            stage_id=stage_id,
        )
    ]


def compose_judge_panel(
    profile: Profile | None,
    stage_id: str = "judge-panel",
    *,
    registry: RoleRegistry | None = None,
    generator_models: tuple[str, ...] = (),
) -> list[ExecutionStage]:
    """Compose the two-judge panel, or nothing at all.

    The two judges see the same pair in opposite orders and never see each
    other's verdict. Agreement on a candidate is a preference; agreement on a
    position is bias, which the fold reports as unresolved rather than as a
    winner. Both are cheap on purpose - a frontier adjudicator is bought only
    after a discriminating test has failed to separate the candidates.
    """
    if profile is None or not profile.artifact_judge:
        return []
    generator_families = {
        family_of(model, registry.provider_catalog)
        for model in generator_models
        if registry is not None
    }
    panel = tuple(
        (role, reason)
        for role, reason in JUDGE_PANEL
        if registry is None
        or (judge := registry.get(role)) is None
        or not judge.model
        or family_of(judge.model, registry.provider_catalog) not in generator_families
    )
    if not panel:
        return [
            ExecutionStage(
                "judge-unjudgeable",
                False,
                "UNJUDGEABLE: every configured judge shares a generator family; "
                "escalate to frontier",
                kind="sentinel",
                stage_id=stage_id,
            )
        ]
    return [
        ExecutionStage(
            "judge-panel",
            True,
            "independent pairwise comparison in reversed order",
            (),
            kind="parallel_spawn",
            stage_id=stage_id,
            members=tuple(
                ExecutionMember(str(index), role, True, reason)
                for index, (role, reason) in enumerate(panel)
            ),
        )
    ]


def compose_judge_rereads(
    profile: Profile | None,
    stage_id: str = "judge-rereads",
) -> list[ExecutionStage]:
    """Compose bounded blind rereads only after the base AB+BA panel disagrees."""
    if profile is None or not profile.artifact_judge or not profile.judge_extra_reads_max:
        return []
    count = max(0, min(3, profile.judge_extra_reads_max))
    return [
        ExecutionStage(
            "judge-rereads",
            True,
            "independent blind rereads after AB+BA disagreement",
            (),
            kind="parallel_spawn",
            stage_id=stage_id,
            members=tuple(
                ExecutionMember(
                    str(index),
                    "judge-disagreement",
                    True,
                    "compare A then B" if index % 2 == 0 else "compare B then A",
                )
                for index in range(count)
            ),
        )
    ]


def compose_judge_challengers(
    profile: Profile | None,
    *,
    registry: RoleRegistry | None = None,
    stage_id: str = "judge-challengers",
) -> list[ExecutionStage]:
    """Compose opt-in eval challengers only when real catalog bindings exist."""
    if profile is None or not profile.judge_challengers:
        return []
    registry = registry or load_registry()
    members = []
    for name, reason in (
        ("judge-challenger-agentic", "agentic and tool-call adversarial challenge"),
        ("judge-challenger-structural", "strict structural ground-truth challenge"),
    ):
        role = registry.get(name)
        meta = registry.provider_catalog.get(role.model) if role and role.model else None
        if role is not None and role.model and meta is not None and meta.endpoints:
            members.append(ExecutionMember(str(len(members)), name, True, reason))
    if not members:
        return []
    return [
        ExecutionStage(
            "judge-challengers",
            True,
            "opt-in challenger eval arm; not part of the production judge pool",
            (),
            kind="parallel_spawn",
            stage_id=stage_id,
            members=tuple(members),
        )
    ]


def compose_evidence_stage(request, stage_id: str = "evidence-collection") -> ExecutionStage:
    """Turn a verifier-planner request into the stage that obtains the evidence.

    The verifier-planner describes the experiment and the harness decides
    whether to pay for it; keeping those two apart is what stops a judge from
    being able to order its own experiments.
    """
    spec = verifier_planner.evidence_stage_spec(request, stage_id)
    reason = str(spec["reason"])
    checks = tuple(str(check) for check in spec.get("checks", ()))
    if checks:
        reason = f"{reason}; targeted checks: {', '.join(checks)}"
    return ExecutionStage(
        str(spec["role"]),
        bool(spec["required"]),
        reason,
        (),
        kind=str(spec["kind"]),
        stage_id=str(spec["stage_id"]),
    )


def compose_adjudication_stage(
    role: str = "judge-disagreement",
    stage_id: str = "judge-adjudication",
    *,
    registry: RoleRegistry | None = None,
    generator_models: tuple[str, ...] = (),
) -> ExecutionStage:
    """Compose the single adjudication stage bought after a panel deadlock."""
    judge = registry.get(role) if registry is not None else None
    generator_families = {
        family_of(model, registry.provider_catalog)
        for model in generator_models
        if registry is not None
    }
    if (
        judge is not None
        and judge.model
        and (family_of(judge.model, registry.provider_catalog) in generator_families)
    ):
        return ExecutionStage(
            "judge-unjudgeable",
            False,
            "UNJUDGEABLE: adjudicator shares a generator family; escalate to frontier",
            kind="sentinel",
            stage_id=stage_id,
        )
    return ExecutionStage(
        role,
        True,
        "adjudicate the comparison the panel could not settle, on the new evidence",
        (),
        kind="judge",
        stage_id=stage_id,
    )
