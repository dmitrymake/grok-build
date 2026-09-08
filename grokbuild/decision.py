#!/usr/bin/env python3
"""Typed, versioned routing decision for the Grok Build combine router.

`RouteDecision` is the single serializable object flowing between the feature
extractor, scorer, policy, runtime state, hook, and CLI. Bumping `SCHEMA_VERSION`
records an intentional wire change; `decision_from_dict` rejects unknown
versions instead of guessing.

No third-party deps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

# The stage vocabulary. `spawn` is a linear child, `parallel_spawn` a barrier of
# independently tracked members, `verify` a deterministic command the runtime
# runs itself, `confirmation_batch` a linear second-pass verifier-family child,
# `sentinel` an unspawnable dead end carrying its own reason, `judge` a pure
# artifact comparison, and `evidence_collection` an experiment the harness
# composes when a comparison could not be settled.
STAGE_KINDS = (
    "spawn",
    "parallel_spawn",
    "verify",
    "confirmation_batch",
    "sentinel",
    "judge",
    "evidence_collection",
)

SCHEMA_VERSION = 4
TELEMETRY_GENERATION = 1

Matches = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class ExecutionMember:
    """One independently tracked member of a parallel execution barrier."""

    member_id: str
    role: str
    required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "role": self.role,
            "required": self.required,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionMember":
        return cls(
            member_id=str(data.get("member_id", "")),
            role=str(data.get("role", "")),
            required=bool(data.get("required", False)),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class ExecutionStage:
    """One linear spawn, deterministic verification, or parallel barrier.

    ``kind`` is the single extension point for new capabilities: every feature
    expresses itself as a stage kind plus policy over this one track, never as a
    second task graph. The values in use are named in :data:`STAGE_KINDS`;
    unknown kinds deserialize and behave as ordinary linear spawns, which keeps
    an older state file readable by a newer harness.
    """

    role: str
    required: bool
    reason: str
    alternatives: tuple[str, ...] = ()
    kind: str = "spawn"
    command: str = ""
    stage_id: str = ""
    members: tuple[ExecutionMember, ...] = ()
    slot: str = ""

    @property
    def spawnable(self) -> bool:
        if self.kind == "confirmation_batch":
            return True
        return self.kind != "verify"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "required": self.required,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
            "kind": self.kind,
            "command": self.command,
            "stage_id": self.stage_id,
            "members": [member.to_dict() for member in self.members],
            "slot": self.slot,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionStage":
        return cls(
            role=str(data.get("role", "")),
            required=bool(data.get("required", False)),
            reason=str(data.get("reason", "")),
            alternatives=tuple(str(x) for x in (data.get("alternatives") or [])),
            kind=str(data.get("kind", "spawn")),
            command=str(data.get("command", "")),
            stage_id=str(data.get("stage_id", "")),
            members=tuple(
                ExecutionMember.from_dict(item)
                for item in (data.get("members") or [])
                if isinstance(item, Mapping)
            ),
            slot=str(data.get("slot", "")),
        )


@dataclass(frozen=True)
class Candidate:
    """One spawnable role considered for a task, with its explainable score."""

    role: str
    score: float
    reasons: tuple[str, ...] = ()
    chosen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "score": self.score,
            "reasons": list(self.reasons),
            "chosen": self.chosen,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Candidate":
        return cls(
            role=str(data.get("role", "")),
            score=float(data.get("score", 0.0)),
            reasons=tuple(str(x) for x in (data.get("reasons") or [])),
            chosen=bool(data.get("chosen", False)),
        )


def _matches_from_mapping(matches: Any) -> Matches:
    if matches is None:
        return ()
    if isinstance(matches, Mapping):
        pairs = []
        for name, values in matches.items():
            pairs.append((str(name), tuple(sorted({str(v) for v in values}))))
        return tuple(sorted(pairs))
    pairs = []
    for item in matches:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            name, values = item
            pairs.append((str(name), tuple(sorted({str(v) for v in values}))))
    return tuple(sorted(pairs))


def _stages_from(value: Any) -> tuple[ExecutionStage, ...]:
    return tuple(
        ExecutionStage.from_dict(item)
        if isinstance(item, Mapping)
        else ExecutionStage("", False, "")
        for item in (value or [])
    )


def _candidates_from(value: Any) -> tuple[Candidate, ...]:
    return tuple(
        Candidate.from_dict(item) if isinstance(item, Mapping) else Candidate("", 0.0)
        for item in (value or [])
    )


@dataclass(frozen=True)
class RouteDecision:
    """One routing outcome.

    `model` is the *required* model for the resolved role; `current_model` is
    the model the conductor is actually on. `role` is the final selected,
    spawnable role after availability/fallback scoring.
    """

    # identity
    version: int = SCHEMA_VERSION
    decision_id: str = ""
    policy_id: str = "default"
    policy_version: int = 1
    repo_id: str | None = None
    turn_id: int | None = None
    prompt_key: str = ""

    # classification
    intent: str | None = None
    task_class: str = "general"
    complexity: str = "low"
    risk: str = "low"
    role: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    how: str | None = None
    block_tools: tuple[str, ...] = ()
    matches: Matches = ()
    strong: tuple[str, ...] = ()
    has_strong: bool = False
    reason: str = "no_intent"
    score: float = 0.0
    confidence: float = 0.0
    second_opinion: dict[str, Any] | None = None

    # execution / selection
    execution: tuple[ExecutionStage, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    fallbacks: tuple[str, ...] = ()
    score_breakdown: dict[str, Any] | None = None
    features: dict[str, Any] | None = None

    # policy / gating
    profile: str = "default"
    mode: str = "static"
    override: str | None = None
    preference: str | None = None
    write_policy: str = "observe"
    role_spawnable: bool = True
    execution_valid: bool = True
    role_available: bool | None = None
    circuit_open: bool = False
    role_quota_used: int | None = None
    allowed: bool = True
    enforce: bool = False
    would_deny_edits: bool = False
    would_block_stop: bool = False
    warnings: tuple[str, ...] = ()

    # provenance
    source: str = "none"
    current_model: str | None = None
    session_id: str | None = None
    event: str = "resolve"
    prompt_chars: int = 0
    stage_trace: tuple[tuple[str, str], ...] = ()
    observed_at: str = ""
    created_at: str = ""

    @property
    def required_model(self) -> str | None:
        return self.model

    @property
    def schema_version(self) -> int:
        return self.version


def decision_to_dict(decision: RouteDecision) -> dict[str, Any]:
    return {
        "version": decision.version,
        "telemetry_generation": TELEMETRY_GENERATION,
        "decision_id": decision.decision_id,
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "repo_id": decision.repo_id,
        "turn_id": decision.turn_id,
        "prompt_key": decision.prompt_key,
        "intent": decision.intent,
        "task_class": decision.task_class,
        "complexity": decision.complexity,
        "risk": decision.risk,
        "role": decision.role,
        "model": decision.model,
        "reasoning_effort": decision.reasoning_effort,
        "how": decision.how,
        "block_tools": list(decision.block_tools),
        "matches": {name: list(values) for name, values in decision.matches},
        "strong": list(decision.strong),
        "has_strong": decision.has_strong,
        "reason": decision.reason,
        "score": decision.score,
        "confidence": decision.confidence,
        "second_opinion": decision.second_opinion,
        "execution": [stage.to_dict() for stage in decision.execution],
        "candidates": [candidate.to_dict() for candidate in decision.candidates],
        "fallbacks": list(decision.fallbacks),
        "score_breakdown": decision.score_breakdown,
        "features": decision.features,
        "profile": decision.profile,
        "mode": decision.mode,
        "override": decision.override,
        "preference": decision.preference,
        "write_policy": decision.write_policy,
        "role_spawnable": decision.role_spawnable,
        "execution_valid": decision.execution_valid,
        "role_available": decision.role_available,
        "circuit_open": decision.circuit_open,
        "role_quota_used": decision.role_quota_used,
        "allowed": decision.allowed,
        "enforce": decision.enforce,
        "would_deny_edits": decision.would_deny_edits,
        "would_block_stop": decision.would_block_stop,
        "warnings": list(decision.warnings),
        "source": decision.source,
        "current_model": decision.current_model,
        "session_id": decision.session_id,
        "event": decision.event,
        "prompt_chars": decision.prompt_chars,
        "stage_trace": dict(decision.stage_trace),
        "observed_at": decision.observed_at,
        "created_at": decision.created_at,
    }


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def decision_from_dict(data: Mapping[str, Any]) -> RouteDecision:
    version = int(data.get("version", SCHEMA_VERSION))
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported RouteDecision version {version}; expected {SCHEMA_VERSION}")
    stage = data.get("stage_trace") or {}
    stage_trace = (
        tuple(sorted((str(k), str(v)) for k, v in stage.items()))
        if isinstance(stage, Mapping)
        else tuple((str(k), str(v)) for k, v in stage)
    )
    return RouteDecision(
        version=version,
        decision_id=str(data.get("decision_id", "")),
        policy_id=str(data.get("policy_id", "default")),
        policy_version=int(data.get("policy_version", 1)),
        repo_id=data.get("repo_id"),
        turn_id=_optional_int(data.get("turn_id")),
        prompt_key=str(data.get("prompt_key", "")),
        intent=data.get("intent"),
        task_class=str(data.get("task_class", "general")),
        complexity=str(data.get("complexity", "low")),
        risk=str(data.get("risk", "low")),
        role=data.get("role"),
        model=data.get("model"),
        reasoning_effort=data.get("reasoning_effort"),
        how=data.get("how"),
        block_tools=tuple(str(x) for x in (data.get("block_tools") or [])),
        matches=_matches_from_mapping(data.get("matches")),
        strong=tuple(str(x) for x in (data.get("strong") or [])),
        has_strong=bool(data.get("has_strong")),
        reason=str(data.get("reason") or "no_intent"),
        score=float(data.get("score") or 0.0),
        confidence=float(data.get("confidence") or 0.0),
        second_opinion=dict(data["second_opinion"])
        if isinstance(data.get("second_opinion"), Mapping)
        else None,
        execution=_stages_from(data.get("execution")),
        candidates=_candidates_from(data.get("candidates")),
        fallbacks=tuple(str(x) for x in (data.get("fallbacks") or [])),
        score_breakdown=data.get("score_breakdown"),
        features=data.get("features"),
        profile=str(data.get("profile", "default")),
        mode=str(data.get("mode") or "static"),
        override=data.get("override"),
        preference=data.get("preference"),
        write_policy=str(data.get("write_policy", "observe")),
        role_spawnable=bool(data.get("role_spawnable", True)),
        execution_valid=bool(data.get("execution_valid", True)),
        role_available=_optional_bool(data.get("role_available")),
        circuit_open=bool(data.get("circuit_open")),
        role_quota_used=_optional_int(data.get("role_quota_used")),
        allowed=bool(data.get("allowed", True)),
        enforce=bool(data.get("enforce")),
        would_deny_edits=bool(data.get("would_deny_edits")),
        would_block_stop=bool(data.get("would_block_stop")),
        warnings=tuple(str(x) for x in (data.get("warnings") or [])),
        source=str(data.get("source") or "none"),
        current_model=data.get("current_model"),
        session_id=data.get("session_id"),
        event=str(data.get("event") or "resolve"),
        prompt_chars=int(data.get("prompt_chars") or 0),
        stage_trace=stage_trace,
        observed_at=str(data.get("observed_at") or ""),
        created_at=str(data.get("created_at") or ""),
    )


def _route_decision_base(decision: RouteDecision) -> dict[str, Any]:
    """Build the shared serialization values used by telemetry views."""
    return {
        "version": decision.version,
        "telemetry_generation": TELEMETRY_GENERATION,
        "decision_id": decision.decision_id,
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "repo_id": decision.repo_id,
        "turn_id": decision.turn_id,
        "prompt_key": decision.prompt_key,
        "observed_at": decision.observed_at,
        "created_at": decision.created_at,
        "event": decision.event,
        "session_id": decision.session_id,
        "current_model": decision.current_model,
        "intent": decision.intent,
        "task_class": decision.task_class,
        "complexity": decision.complexity,
        "risk": decision.risk,
        "model": decision.model,
        "reasoning_effort": decision.reasoning_effort,
        "role": decision.role,
        "how": decision.how,
        "block_tools": list(decision.block_tools),
        "matches": {name: list(values) for name, values in decision.matches},
        "strong": list(decision.strong),
        "has_strong": decision.has_strong,
        "reason": decision.reason,
        "allowed": decision.allowed,
        "enforce": decision.enforce,
        "would_deny_edits": decision.would_deny_edits,
        "would_block_stop": decision.would_block_stop,
        "prompt_chars": decision.prompt_chars,
        "source": decision.source,
        "mode": decision.mode,
        "profile": decision.profile,
        "score": decision.score,
        "confidence": decision.confidence,
        "second_opinion": decision.second_opinion,
        "override": decision.override,
        "preference": decision.preference,
        "write_policy": decision.write_policy,
        "role_spawnable": decision.role_spawnable,
        "execution_valid": decision.execution_valid,
        "role_available": decision.role_available,
        "circuit_open": decision.circuit_open,
        "role_quota_used": decision.role_quota_used,
        "execution": [stage.to_dict() for stage in decision.execution],
        "candidates": [candidate.to_dict() for candidate in decision.candidates],
        "fallbacks": list(decision.fallbacks),
        "score_breakdown": decision.score_breakdown,
        "warnings": list(decision.warnings),
        "stage_trace": dict(decision.stage_trace),
    }


def decision_hook_record(decision: RouteDecision) -> dict[str, Any]:
    """Compatibility dict for the legacy hook helpers plus the new fields.

    Legacy helpers read `intent` / `required_model` / `current_model` /
    `has_strong` / `how` / `session_id`; every key is present.
    """

    base = _route_decision_base(decision)
    return {
        "version": base["version"],
        "telemetry_generation": base["telemetry_generation"],
        "decision_id": base["decision_id"],
        "policy_id": base["policy_id"],
        "policy_version": base["policy_version"],
        "repo_id": base["repo_id"],
        "turn_id": base["turn_id"],
        "prompt_key": base["prompt_key"],
        "observed_at": base["observed_at"],
        "created_at": base["created_at"],
        "event": base["event"],
        "session_id": base["session_id"],
        "current_model": base["current_model"],
        "intent": base["intent"],
        "task_class": base["task_class"],
        "complexity": base["complexity"],
        "risk": base["risk"],
        "required_model": base["model"],
        "required_effort": base["reasoning_effort"],
        "role": base["role"],
        "how": base["how"],
        "block_tools": base["block_tools"],
        "matches": base["matches"],
        "strong": base["strong"],
        "has_strong": base["has_strong"],
        "reason": base["reason"],
        "allowed": base["allowed"],
        "enforce": base["enforce"],
        "would_deny_edits": base["would_deny_edits"],
        "would_block_stop": base["would_block_stop"],
        "prompt_chars": base["prompt_chars"],
        "source": base["source"],
        "mode": base["mode"],
        "profile": base["profile"],
        "score": base["score"],
        "confidence": base["confidence"],
        "second_opinion": base["second_opinion"],
        "override": base["override"],
        "preference": base["preference"],
        "write_policy": base["write_policy"],
        "role_spawnable": base["role_spawnable"],
        "execution_valid": base["execution_valid"],
        "role_available": base["role_available"],
        "circuit_open": base["circuit_open"],
        "role_quota_used": base["role_quota_used"],
        "execution": base["execution"],
        "candidates": base["candidates"],
        "fallbacks": base["fallbacks"],
        "score_breakdown": base["score_breakdown"],
        "warnings": base["warnings"],
        "stage_trace": base["stage_trace"],
    }


def hash_id(*parts: str) -> str:
    """Deterministic, short decision/prompt id from stable inputs."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]


@dataclass(frozen=True)
class ConductorHandoff:
    """Typed pre-session conductor choice emitted by the launcher resolver.

    Never contains a secret. `model` is the model id to pass to `grok -m`;
    `degraded` marks a fallback off the configured default (glm-5.3-flash).
    """

    model: str
    provider: str = "unknown"
    provider_label: str = "unknown"
    reason: str = "default"
    degraded: bool = False
    credential_present: bool = False
    available: bool | None = None
    circuit_open: bool = False
    source: str = "state"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "reason": self.reason,
            "degraded": self.degraded,
            "credential_present": self.credential_present,
            "available": self.available,
            "circuit_open": self.circuit_open,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConductorHandoff":
        return cls(
            model=str(data.get("model", "")),
            provider=str(data.get("provider", "unknown")),
            provider_label=str(data.get("provider_label", "unknown")),
            reason=str(data.get("reason", "default")),
            degraded=bool(data.get("degraded", False)),
            credential_present=bool(data.get("credential_present", False)),
            available=_optional_bool(data.get("available")),
            circuit_open=bool(data.get("circuit_open", False)),
            source=str(data.get("source", "state")),
        )


__all__ = [
    "SCHEMA_VERSION",
    "RouteDecision",
    "ExecutionMember",
    "ExecutionStage",
    "Candidate",
    "ConductorHandoff",
    "decision_to_dict",
    "decision_from_dict",
    "decision_hook_record",
    "hash_id",
]
