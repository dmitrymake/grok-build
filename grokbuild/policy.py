#!/usr/bin/env python3
"""Configurable policy, profiles, scoring, and explicit prompt overrides.

The default profile reproduces `classify.py` exactly (strong weight 3, phrase
weight 1, topic weight 1, strong anchors beat phrase-only, then `priority`
breaks ties). Profiles live in `profiles.json`; an explicit `route=<intent>`
override bypasses scoring when `allow_override` is set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from grokbuild.features import FeatureSet

PROFILES_PATH = Path(__file__).resolve().parent / "profiles.json"

POLICY_VERSION = 1

# Intents whose `block_tools` is non-empty are edit-gated while a required
# execution stage is unfinished. Model equality does not bypass delegation.


@dataclass(frozen=True)
class Profile:
    name: str
    mode: str = "dynamic"
    strong_weight: float = 3.0
    phrase_weight: float = 1.0
    topic_weight: float = 1.0
    allow_override: bool = True
    require_strong_for_gate: bool = True
    gate_phrase_intents: tuple[str, ...] = ()
    priority: tuple[str, ...] = ()
    min_score: float = 0.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 300.0
    consilium_after_failures: int = 3
    barrier_stall_seconds: float = 1800.0
    stop_block_limit: int = 3
    debt_window_seconds: float = 86400.0
    recon_barrier_width: int = 3
    research_barrier_width: int = 3
    review_barrier_width: int = 1
    recon_advisor_role: str | None = None
    evidence_policy: str | None = None
    planner_market: bool | None = None
    planner_market_width: int = 3
    artifact_judge: bool | None = None
    judge_extra_reads_max: int | None = None
    judge_abstain_margin: float | None = None
    judge_canaries: bool | None = None
    judge_challengers: bool | None = None
    frontier_escalation: bool | None = None
    confirmation_batch: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "mode": self.mode,
            "strong_weight": self.strong_weight,
            "phrase_weight": self.phrase_weight,
            "topic_weight": self.topic_weight,
            "allow_override": self.allow_override,
            "require_strong_for_gate": self.require_strong_for_gate,
            "gate_phrase_intents": list(self.gate_phrase_intents),
            "priority": list(self.priority),
            "min_score": self.min_score,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_cooldown_seconds": self.circuit_cooldown_seconds,
            "consilium_after_failures": self.consilium_after_failures,
            "barrier_stall_seconds": self.barrier_stall_seconds,
            "stop_block_limit": self.stop_block_limit,
            "debt_window_seconds": self.debt_window_seconds,
            "recon_barrier_width": self.recon_barrier_width,
            "research_barrier_width": self.research_barrier_width,
            "review_barrier_width": self.review_barrier_width,
        }
        if self.recon_advisor_role is not None:
            data["recon_advisor_role"] = self.recon_advisor_role
        if self.evidence_policy is not None:
            data["evidence_policy"] = self.evidence_policy
        if self.planner_market is not None:
            data["planner_market"] = self.planner_market
        data["planner_market_width"] = self.planner_market_width
        if self.artifact_judge is not None:
            data["artifact_judge"] = self.artifact_judge
        if self.judge_extra_reads_max is not None:
            data["judge_extra_reads_max"] = self.judge_extra_reads_max
        if self.judge_abstain_margin is not None:
            data["judge_abstain_margin"] = self.judge_abstain_margin
        if self.judge_canaries is not None:
            data["judge_canaries"] = self.judge_canaries
        if self.judge_challengers is not None:
            data["judge_challengers"] = self.judge_challengers
        if self.frontier_escalation is not None:
            data["frontier_escalation"] = self.frontier_escalation
        if self.confirmation_batch is not None:
            data["confirmation_batch"] = self.confirmation_batch
        return data


def _merge_profile(name: str, raw: Mapping[str, Any], base: Profile) -> Profile:
    return Profile(
        name=name,
        mode=str(raw.get("mode", base.mode)),
        strong_weight=float(raw.get("strong_weight", base.strong_weight)),
        phrase_weight=float(raw.get("phrase_weight", base.phrase_weight)),
        topic_weight=float(raw.get("topic_weight", base.topic_weight)),
        allow_override=bool(raw.get("allow_override", base.allow_override)),
        require_strong_for_gate=bool(
            raw.get("require_strong_for_gate", base.require_strong_for_gate)
        ),
        gate_phrase_intents=tuple(
            str(x) for x in raw.get("gate_phrase_intents", base.gate_phrase_intents)
        ),
        priority=tuple(str(x) for x in (raw.get("priority") or base.priority)),
        min_score=float(raw.get("min_score", base.min_score)),
        circuit_failure_threshold=int(
            raw.get("circuit_failure_threshold", base.circuit_failure_threshold)
        ),
        circuit_cooldown_seconds=float(
            raw.get("circuit_cooldown_seconds", base.circuit_cooldown_seconds)
        ),
        consilium_after_failures=int(
            raw.get("consilium_after_failures", base.consilium_after_failures)
        ),
        barrier_stall_seconds=float(raw.get("barrier_stall_seconds", base.barrier_stall_seconds)),
        stop_block_limit=int(raw.get("stop_block_limit", base.stop_block_limit)),
        debt_window_seconds=float(raw.get("debt_window_seconds", base.debt_window_seconds)),
        recon_barrier_width=int(raw.get("recon_barrier_width", base.recon_barrier_width)),
        research_barrier_width=int(raw.get("research_barrier_width", base.research_barrier_width)),
        review_barrier_width=int(raw.get("review_barrier_width", base.review_barrier_width)),
        recon_advisor_role=(
            str(raw["recon_advisor_role"])
            if raw.get("recon_advisor_role") is not None
            else base.recon_advisor_role
        ),
        evidence_policy=(
            str(raw["evidence_policy"])
            if raw.get("evidence_policy") is not None
            else base.evidence_policy
        ),
        planner_market=(
            bool(raw["planner_market"])
            if raw.get("planner_market") is not None
            else base.planner_market
        ),
        planner_market_width=max(
            3, min(4, int(raw.get("planner_market_width", base.planner_market_width)))
        ),
        artifact_judge=(
            bool(raw["artifact_judge"])
            if raw.get("artifact_judge") is not None
            else base.artifact_judge
        ),
        judge_extra_reads_max=(
            max(0, min(3, int(raw["judge_extra_reads_max"])))
            if raw.get("judge_extra_reads_max") is not None
            else base.judge_extra_reads_max
        ),
        judge_abstain_margin=(
            max(0.0, min(1.0, float(raw["judge_abstain_margin"])))
            if raw.get("judge_abstain_margin") is not None
            else base.judge_abstain_margin
        ),
        judge_canaries=(
            bool(raw["judge_canaries"])
            if raw.get("judge_canaries") is not None
            else base.judge_canaries
        ),
        judge_challengers=(
            bool(raw["judge_challengers"])
            if raw.get("judge_challengers") is not None
            else base.judge_challengers
        ),
        frontier_escalation=(
            bool(raw["frontier_escalation"])
            if raw.get("frontier_escalation") is not None
            else base.frontier_escalation
        ),
        confirmation_batch=(
            bool(raw["confirmation_batch"])
            if raw.get("confirmation_batch") is not None
            else base.confirmation_batch
        ),
    )


DEFAULT_PROFILES: dict[str, Profile] = {
    "default": Profile(
        name="default",
        mode="dynamic",
        strong_weight=3.0,
        phrase_weight=1.0,
        topic_weight=1.0,
        allow_override=True,
        require_strong_for_gate=True,
        gate_phrase_intents=("implement",),
        priority=(),
        min_score=0.0,
        consilium_after_failures=3,
        recon_barrier_width=3,
        research_barrier_width=3,
        review_barrier_width=1,
        stop_block_limit=3,
        debt_window_seconds=86400.0,
    ),
}


def load_profiles(path: Path | str | None = None) -> dict[str, Profile]:
    target = Path(path) if path is not None else PROFILES_PATH
    profiles = dict(DEFAULT_PROFILES)
    if not target.is_file():
        return profiles
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return profiles
    raw = data.get("profiles", {}) if isinstance(data, Mapping) else {}
    for name, spec in raw.items():
        base = profiles.get(str(name), DEFAULT_PROFILES["default"])
        profiles[str(name)] = _merge_profile(
            str(name), spec if isinstance(spec, Mapping) else {}, base
        )
    return profiles


@dataclass(frozen=True)
class ScoredIntent:
    name: str
    strong: tuple[str, ...]
    phrases: tuple[str, ...]
    topics: tuple[str, ...]
    score: float


def _hits_for(features: FeatureSet, name: str, key: str) -> tuple[str, ...]:
    table = {
        "strong": features.strong,
        "phrases": features.phrases,
        "topics": features.topics,
    }[key]
    for intent_name, values in table:
        if intent_name == name:
            return values
    return ()


def score_intents(
    features: FeatureSet,
    spec: Mapping[str, Any],
    profile: Profile,
) -> dict[str, ScoredIntent]:
    scored: dict[str, ScoredIntent] = {}
    intents = spec.get("intents", {}) if isinstance(spec, Mapping) else {}
    for name in intents:
        strong = _hits_for(features, str(name), "strong")
        phrases = _hits_for(features, str(name), "phrases")
        topics = _hits_for(features, str(name), "topics")
        # Product names (firmware, vpn, …) never fire a role by themselves:
        # topics-only intents are not scorable, matching classify.py.
        if not strong and not phrases:
            continue
        score = (
            profile.strong_weight * len(strong)
            + profile.phrase_weight * len(phrases)
            + profile.topic_weight * len(topics)
        )
        scored[str(name)] = ScoredIntent(str(name), strong, phrases, topics, score)
    return scored


def pick_winner(
    scored: Mapping[str, ScoredIntent],
    priority: tuple[str, ...],
) -> str | None:
    if not scored:
        return None
    strong_first = [name for name in priority if name in scored and scored[name].strong]
    if not strong_first:
        strong_first = [name for name, value in scored.items() if value.strong]
    if strong_first:
        return strong_first[0]
    for name in priority:
        if name in scored:
            return name
    # Deterministic fallback: highest score, then insertion order.
    return max(scored, key=lambda name: (scored[name].score, -list(scored).index(name)))


def gated_intents(spec: Mapping[str, Any]) -> frozenset[str]:
    intents = spec.get("intents", {}) if isinstance(spec, Mapping) else {}
    return frozenset(
        str(name)
        for name, intent in intents.items()
        if isinstance(intent, Mapping) and (intent.get("block_tools") or [])
    )


def resolve_mode_from_env(
    env: Mapping[str, str],
    profile: Profile,
) -> str:
    """Map environment to one of static|shadow|dynamic.

    Precedence: GROK_ROUTE_MODE > GROK_ROUTE_ENFORCE > GROK_ROUTE_SOFT >
    profile.mode. The legacy flags keep their historical meaning.
    """

    raw = (env.get("GROK_ROUTE_MODE") or "").strip().casefold()
    if raw in {"static", "shadow", "dynamic"}:
        return raw
    if is_truthy_env(env, "GROK_ROUTE_ENFORCE"):
        return "dynamic"
    if (env.get("GROK_ROUTE_SOFT") or "").strip() and not is_truthy_env(env, "GROK_ROUTE_SOFT"):
        return "shadow"  # GROK_ROUTE_SOFT=0 restores observe-only edits
    if profile.mode in {"static", "shadow", "dynamic"}:
        return profile.mode
    return "dynamic"  # historical default: soft deny is a dynamic behaviour


def is_truthy_env(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip() not in {"", "0", "false", "False", "no"}


__all__ = [
    "Profile",
    "ScoredIntent",
    "load_profiles",
    "score_intents",
    "pick_winner",
    "gated_intents",
    "resolve_mode_from_env",
    "is_truthy_env",
    "DEFAULT_PROFILES",
    "PROFILES_PATH",
    "POLICY_VERSION",
]
