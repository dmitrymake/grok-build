#!/usr/bin/env python3
"""Declarative spawnable-role registry, validated against config.toml.

Grok Build resolves `spawn_subagent` by *agent type* (role name), never by a
model slug. This module reads `[subagents.roles.*]` and `[subagents.models.*]`
from `~/.grok/config.toml`, layers them over the built-in agent types, then
validates that every intent in `intents.json` points at a role that exists and
whose declared metadata is consistent.

Capability metadata is taken from config where present and is explicitly
`None`/`"unknown"` otherwise — the registry never fabricates cost/latency/quality
numbers. A few fields are derived from facts that *are* in config:
`write` from `default_capability_mode`, `provider`/`context` from the model block,
and `security`/`review` from the role name. Provider pool + tier metadata comes
from `providers.json` (the single source of truth for provider/auth/tier info),
falling back to the model block `base_url` host when a model is unlisted.

If config.toml is missing or unreadable the registry falls back to the built-in
types plus a warning — it never raises on a broken config at hook time.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ._repo import config_path as repo_config_path
from typing import Any, Mapping

PROVIDERS_PATH = Path(__file__).resolve().parent / "providers.json"

# Public spawnable aliases kept for backward compatibility. The router resolves
# these to the canonical role for scoring/tracking but the alias name remains a
# valid spawn_subagent type because it is still declared in config.toml.
ROLE_ALIASES: dict[str, str] = {
    "implement": "implement-standard",
    "review": "review-hard",
    "plan": "plan-hard",
}

VALID_RISK_CEILINGS = frozenset({"low", "medium", "high"})
VALID_AUTONOMY = frozenset({"guided", "standard", "broad"})

CANONICAL_REASONING_EFFORTS = frozenset(
    {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
)

# Canonical roles that must never be writable (analysis/verification only).
READ_ONLY_CANONICAL = frozenset(
    {
        "visual-intake",
        "visual-intake-deep",
        "review",
        "review-hard",
        "review-independent",
        "expert-rescue",
        "consilium-analyst",
        "consilium-challenger",
        "consilium-arbiter",
        "security",
        "security-verify",
        "plan",
        "plan-hard",
        "explore",
        "explore-thorough",
        "explore-risk",
        "researcher",
        "researcher-analyst",
        "researcher-challenger",
        "planner-a",
        "planner-b",
        "planner-c",
        "plan-comparator",
        "criterion-judge",
        "judge-primary",
        "judge-independent",
        "judge-disagreement",
        "judge-frontier-code",
        "judge-frontier-general",
        "judge-challenger-agentic",
        "judge-challenger-structural",
        "verifier-planner",
        "frontier-resolver",
        "frontier-resolver-standby",
    }
)

VISUAL_INTAKE_ROLES = frozenset({"visual-intake", "visual-intake-deep"})
RECON_ROLES = frozenset({"explore", "explore-thorough", "explore-risk"})
EXECUTION_READ_ONLY_ROLES = READ_ONLY_CANONICAL - VISUAL_INTAKE_ROLES

# Writable implementation roles that the registry must declare as capable of
# writing (a misconfigured Flash fallback that does not register as writable is
# rejected here, never silently accepted as a write-capable executor).
IMPLEMENT_ROLE_NAMES = frozenset(
    {
        "implement",
        "implement-cheap",
        "implement-standard",
        "implement-strong",
        "implement-hard",
        "implement-ops",
        "implement-overflow",
        "implement-cheap-fallback",
    }
)

# name -> (model, capability_mode, description, source)
_BUILTIN_ROLES_FALLBACK: dict[str, dict[str, Any]] = {
    "general-purpose": {
        "model": None,
        "capability_mode": "all",
        "description": "Full-capability agent; inherits the parent model.",
    },
    "explore": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "capability_mode": "read-only",
        "description": "Scoped reconnaissance agent; reads and greps, never edits.",
    },
    "explore-thorough": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Architecture-wide reconnaissance agent; never edits.",
    },
    "explore-risk": {
        "model": "minimax-m3",
        "reasoning_effort": "high",
        "capability_mode": "read-only",
        "description": "Bounded risk-recon advisor; never edits.",
    },
    "plan": {
        "model": "gpt-6-astra",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Planning agent; produces a structured plan, never edits.",
    },
    "plan-hard": {
        "model": "gpt-6-astra",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Hard read-only planning.",
    },
    "implement": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "capability_mode": "all",
        "description": "Standard implementation alias.",
    },
    "implement-cheap": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "capability_mode": "all",
        "description": "Low-scope implementation.",
    },
    "implement-standard": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "capability_mode": "all",
        "description": "Standard implementation.",
    },
    "implement-hard": {
        "model": "gpt-6-astra",
        "reasoning_effort": "max",
        "capability_mode": "all",
        "description": "Hard implementation.",
    },
    "implement-strong": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "capability_mode": "all",
        "description": "Strong implementation for medium-complexity work.",
    },
    "planner-strong": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Optional strong read-only planner seat for the expanded planner market.",
    },
    "implement-ops": {
        "model": "glm-5.3",
        "reasoning_effort": "high",
        "capability_mode": "all",
        "description": "Operations implementation.",
    },
    "implement-overflow": {
        "model": "deepseek-v4-pro",
        "reasoning_effort": "high",
        "capability_mode": "all",
        "description": "Cross-provider overflow implementation.",
        "tier": "overflow",
    },
    "implement-cheap-fallback": {
        "model": "deepseek-v4-flash",
        "reasoning_effort": "high",
        "capability_mode": "all",
        "description": "Last-resort writable fallback.",
    },
    "review": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "capability_mode": "read-only",
        "description": "Internal review alias.",
    },
    "review-hard": {
        "model": "glm-5.3",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Internal review.",
    },
    "review-independent": {
        "model": "grok-4.6",
        "capability_mode": "read-only",
        "description": "Independent review.",
    },
    "expert-rescue": {
        "model": "deepseek-v4-pro",
        "reasoning_effort": "high",
        "capability_mode": "read-only",
        "description": "Independent diagnosis.",
    },
    "consilium-analyst": {
        "model": "glm-5.3",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Fresh-eyes failure analysis.",
    },
    "consilium-challenger": {
        "model": "deepseek-v4-pro",
        "reasoning_effort": "high",
        "capability_mode": "read-only",
        "description": "Adversarial repair cross-examination.",
    },
    "consilium-arbiter": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "capability_mode": "read-only",
        "description": "Repair verdict and plan synthesis.",
    },
    "security": {
        "model": "glm-5.3",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Security analysis.",
    },
    "security-verify": {
        "model": "grok-4.6",
        "capability_mode": "read-only",
        "description": "Independent security verification with explicit OAuth availability verification.",
    },
    "visual-intake": {
        "model": "minimax-m3",
        "capability_mode": "read-only",
        "description": "Structured visual intake.",
    },
    "planner-a": {
        "model": "deepseek-v4-pro",
        "capability_mode": "read-only",
        "description": "Independent read-only task decomposition (primary provider).",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "planner-b": {
        "model": "glm-5.3",
        "capability_mode": "read-only",
        "description": "Independent read-only task decomposition (redundant provider).",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "broad",
    },
    "planner-c": {
        "model": "gpt-5.6-luna",
        "capability_mode": "read-only",
        "description": "Independent read-only task decomposition (different model family).",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "plan-comparator": {
        "model": "gemini-3.7-flash",
        "capability_mode": "read-only",
        "description": "Reads competing decompositions and reports agreement; produces a signal, never a selection.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "criterion-judge": {
        "model": "glm-5.3-flash",
        "capability_mode": "read-only",
        "description": "Evaluates one contract criterion against structured evidence and marks only disputed criteria for escalation.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "judge-primary": {
        "model": "gemini-3.7-flash",
        "capability_mode": "read-only",
        "description": "Compares candidate artifacts pairwise on evidence. Sees no model, provider or author identity.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "judge-independent": {
        "model": "glm-5.3-flash",
        "capability_mode": "read-only",
        "description": "Second independent comparison of the same pair in reversed order, from a different provider.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "judge-disagreement": {
        "model": "gpt-5.6-luna",
        "capability_mode": "read-only",
        "description": "Adjudicates only when the two cheap judges disagree after a discriminating test.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "judge-frontier-code": {
        "model": "grok-4.6",
        "capability_mode": "read-only",
        "description": "Frontier adjudication for code artifacts, bought only after cheaper comparison fails to resolve.",
        "tier": "cheap",
        "reasoning_effort": None,
        "autonomy": "standard",
    },
    "judge-frontier-general": {
        "model": "gpt-5.6-sol",
        "capability_mode": "read-only",
        "description": "Frontier adjudication for non-code artifacts, bought only after cheaper comparison fails to resolve.",
        "tier": "cheap",
        "reasoning_effort": "xhigh",
        "autonomy": "standard",
    },
    "judge-challenger-agentic": {
        "model": "gpt-oss-120b",
        "reasoning_effort": "low",
        "capability_mode": "read-only",
        "description": "Opt-in agentic challenger for GPT-OSS-120B via Together AI.",
        "autonomy": "standard",
    },
    "judge-challenger-structural": {
        "model": "gpt-oss-120b",
        "reasoning_effort": "low",
        "capability_mode": "read-only",
        "description": "Opt-in structural challenger for GPT-OSS-120B via Together AI.",
        "autonomy": "standard",
    },
    "verifier-planner": {
        "model": "glm-5.3-flash",
        "capability_mode": "read-only",
        "description": "States what evidence would settle an undecided comparison. Requests the experiment; never runs it.",
        "tier": "cheap",
        "reasoning_effort": "high",
        "autonomy": "standard",
    },
    "frontier-resolver": {
        "model": "grok-4.6",
        "capability_mode": "read-only",
        "description": "Rare meta-planner: frames an ambiguous task, decomposes it and names the invariants. Never executes the work.",
        "tier": "spare",
        "reasoning_effort": None,
        "fallback": ["frontier-resolver-standby"],
        "autonomy": "standard",
    },
    "frontier-resolver-standby": {
        "model": "gpt-5.6-sol",
        "capability_mode": "read-only",
        "description": "Standby binding for the frontier resolver when its primary is unavailable. Same meta-planning duty.",
        "tier": "spare",
        "reasoning_effort": "xhigh",
        "autonomy": "standard",
    },
    "researcher": {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Deep multi-source research and synthesis lead.",
    },
    "researcher-analyst": {
        "model": "glm-5.3",
        "reasoning_effort": "max",
        "capability_mode": "read-only",
        "description": "Alternative-perspective research analysis.",
    },
    "researcher-challenger": {
        "model": "grok-4.6",
        "capability_mode": "read-only",
        "description": "Adversarial verification of research findings.",
    },
    "visual-intake-deep": {
        "model": "gpt-5.6-terra",
        "capability_mode": "read-only",
        "description": "High-detail structured visual intake.",
    },
}


def _load_builtin_roles() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(
            (Path(__file__).resolve().parent / "roles_default.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict) and data:
            return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return _BUILTIN_ROLES_FALLBACK.copy()


BUILTIN_ROLES: dict[str, dict[str, Any]] = _load_builtin_roles()

TASK_BY_ROLE_NAME = {
    "visual-intake": ("visual",),
    "visual-intake-deep": ("visual",),
    "security": ("security",),
    "security-verify": ("security",),
    "implement": ("coding", "ops"),
    "implement-cheap": ("coding", "ops"),
    "implement-standard": ("coding", "ops"),
    "implement-strong": ("coding", "ops"),
    "implement-hard": ("coding", "ops"),
    "implement-ops": ("coding", "ops"),
    "implement-overflow": ("coding", "ops"),
    "implement-cheap-fallback": ("coding", "ops"),
    "review": ("review",),
    "review-hard": ("review",),
    "review-independent": ("review",),
    "expert-rescue": ("review",),
    "consilium-analyst": ("review",),
    "consilium-challenger": ("review",),
    "consilium-arbiter": ("review",),
    "plan": ("planning",),
    "plan-hard": ("planning",),
    "planner-a": ("planning",),
    "planner-b": ("planning",),
    "planner-c": ("planning",),
    "planner-strong": ("planning",),
    "plan-comparator": ("planning",),
    "criterion-judge": ("review",),
    "judge-primary": ("review",),
    "judge-independent": ("review",),
    "judge-disagreement": ("review",),
    "judge-frontier-code": ("review",),
    "judge-frontier-general": ("review",),
    "judge-challenger-agentic": ("review",),
    "judge-challenger-structural": ("review",),
    "verifier-planner": ("planning",),
    "frontier-resolver": ("planning",),
    "frontier-resolver-standby": ("planning",),
    "researcher": ("research",),
    "researcher-analyst": ("research",),
    "researcher-challenger": ("research",),
    "explore": ("research",),
    "explore-thorough": ("research",),
    "explore-risk": ("research",),
    "general-purpose": ("general",),
}


def credential_present(meta: Any, env: Mapping[str, str]) -> bool:
    """Return credential presence without reading or emitting credential values."""
    if meta is None:
        return True
    credential_env = getattr(meta, "credential_env", None)
    if credential_env:
        return bool((env.get(credential_env) or "").strip())
    credential_path = getattr(meta, "credential_path", None)
    if credential_path:
        path = str(credential_path)
        if path.startswith("~"):
            # HOME from the supplied mapping is intentional for hermetic checks.
            home = Path(env.get("HOME") or Path.home())
            path = str(home) + path[1:]
        return Path(path).is_file()
    return True


def role_credential_present(role: Any, meta: Any, env: Mapping[str, str]) -> bool:
    """Resolve a role credential from catalog metadata or its config model block."""
    if meta is not None:
        return credential_present(meta, env)
    credential_env = getattr(role, "credential_env", None)
    return not credential_env or bool((env.get(credential_env) or "").strip())


def provider_from(base_url: Any) -> str:
    if not base_url:
        return "unknown"
    try:
        from urllib.parse import urlparse

        host = urlparse(str(base_url)).hostname or ""
        return host or "unknown"
    except ValueError:
        return "unknown"


@dataclass(frozen=True)
class Capabilities:
    text: bool | None = None
    vision: bool | None = None
    audio: bool | None = None
    video: bool | None = None
    tools: bool | None = None
    structured_output: bool | None = None
    logprobs: bool | None = None

    def to_dict(self) -> dict[str, bool | None]:
        return {
            "text": self.text,
            "vision": self.vision,
            "audio": self.audio,
            "video": self.video,
            "tools": self.tools,
            "structured_output": self.structured_output,
            "logprobs": self.logprobs,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Capabilities":
        if not isinstance(data, Mapping):
            return cls()
        keys = ("text", "vision", "audio", "video", "tools", "structured_output", "logprobs")
        values: dict[str, bool | None] = {}
        for key in keys:
            value = data.get(key)
            values[key] = value if isinstance(value, bool) else None
        return cls(**values)


@dataclass(frozen=True)
class ProviderEndpointMeta:
    provider: str
    provider_label: str
    model_binding: str | None = None
    subscription_class: str | None = None
    require_availability_signal: bool = False
    auth: str | None = None
    credential_env: str | None = None
    credential_path: str | None = None
    availability_key: str | None = None


@dataclass(frozen=True)
class ProviderModelMeta:
    provider: str
    provider_label: str
    tier: str | None
    cost: float | None
    latency: float | None
    quality_prior: float | None
    family: str = "unknown"
    subscription_class: str | None = None
    require_availability_signal: bool = False
    auth: str | None = None
    credential_env: str | None = None
    credential_path: str | None = None
    capabilities: Capabilities = Capabilities()
    endpoints: tuple[ProviderEndpointMeta, ...] = ()
    explicit_endpoints: bool = False
    balance: str = "ordered"


def load_provider_catalog(path: Path | str | None = None) -> dict[str, ProviderModelMeta]:
    """Return logical model metadata with ordered provider endpoints."""
    target = Path(path) if path is not None else PROVIDERS_PATH
    out: dict[str, ProviderModelMeta] = {}
    if not target.is_file():
        return out
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    providers = data.get("providers", {}) if isinstance(data, Mapping) else {}
    if not isinstance(providers, Mapping):
        return out

    def endpoint(provider: str, override: Mapping[str, Any] | None = None) -> ProviderEndpointMeta:
        override = override or {}
        raw = providers.get(provider, {})
        spec = raw if isinstance(raw, Mapping) else {}

        def value(key: str) -> str | None:
            item = override.get(key, spec.get(key))
            return str(item) if item is not None else None

        return ProviderEndpointMeta(
            provider=provider,
            provider_label=str(override.get("label") or spec.get("label") or provider),
            model_binding=value("model_binding"),
            subscription_class=value("subscription_class"),
            require_availability_signal=bool(
                override.get(
                    "require_availability_signal",
                    spec.get("require_availability_signal", False),
                )
            ),
            auth=value("auth"),
            credential_env=value("credential_env"),
            credential_path=value("credential_path"),
            availability_key=value("availability_key"),
        )

    for pkey, pspec in providers.items():
        if not isinstance(pspec, Mapping):
            continue
        models = pspec.get("models", {})
        if not isinstance(models, Mapping):
            continue
        for mid, mspec in models.items():
            if not isinstance(mspec, Mapping):
                continue
            raw_endpoints = mspec.get("endpoints")
            explicit = isinstance(raw_endpoints, list)
            endpoints: list[ProviderEndpointMeta] = []
            if explicit:
                for raw_endpoint in raw_endpoints:
                    if not isinstance(raw_endpoint, Mapping) or not raw_endpoint.get("provider"):
                        continue
                    endpoints.append(endpoint(str(raw_endpoint["provider"]), raw_endpoint))
            if not endpoints:
                endpoints.append(endpoint(str(pkey)))
            primary = endpoints[0]

            def _opt_float(key: str) -> float | None:
                value = mspec.get(key)
                return None if value is None else float(value)

            out[str(mid)] = ProviderModelMeta(
                provider=primary.provider,
                provider_label=primary.provider_label,
                family=str(mspec.get("family") or mid),
                subscription_class=primary.subscription_class,
                tier=str(mspec.get("tier")) if mspec.get("tier") is not None else None,
                cost=_opt_float("cost"),
                latency=_opt_float("latency"),
                quality_prior=_opt_float("quality_prior"),
                require_availability_signal=primary.require_availability_signal,
                auth=primary.auth,
                credential_env=primary.credential_env,
                credential_path=primary.credential_path,
                capabilities=Capabilities.from_dict(mspec.get("capabilities")),
                endpoints=tuple(endpoints),
                explicit_endpoints=explicit,
                balance=(
                    str(mspec["balance"])
                    if mspec.get("balance") in {"ordered", "rotate"}
                    else "ordered"
                ),
            )
    return out


def family_of(
    model: str | None,
    provider_catalog: Mapping[str, ProviderModelMeta] | None = None,
) -> str | None:
    """Return the model family: catalog-declared, else a line-name fallback.

    Models outside the packaged catalog (private config pins such as
    gpt-5.6-sol, or gateway aliases such as qwen3.8-max@commandcode) must not
    silently become one-model families: an ``@provider`` alias belongs to its
    base model, and an alphabetic line suffix after a versioned stem names a
    variant of the same line (gpt-5.6-sol and gpt-5.6-luna share training
    lineage, which is exactly what the independence rules guard against).
    The fallback mirrors the catalog's own family scheme (deepseek-v4,
    qwen3.8, glm-5.3); a catalog entry always wins.
    """
    if not model:
        return None
    catalog = load_provider_catalog() if provider_catalog is None else provider_catalog
    meta = catalog.get(model)
    if meta is not None:
        return meta.family
    base = model.split("@", 1)[0]
    meta = catalog.get(base)
    if meta is not None:
        return meta.family
    stem, sep, suffix = base.rpartition("-")
    if sep and suffix.isalpha() and any(ch.isdigit() for ch in stem):
        return stem
    return base


@dataclass(frozen=True)
class ExecutableModelBinding:
    model: str
    base_url: str | None
    credential_env: str | None


@dataclass(frozen=True)
class Role:
    name: str
    model: str | None
    capability_mode: str = "all"
    description: str = ""
    reasoning_effort: str | None = None
    autonomy: str = "standard"
    source: str = "builtin"
    task: tuple[str, ...] | None = None
    support: str | None = None
    preference: int | None = None
    quality_prior: float | None = None
    latency: float | None = None
    quota: float | None = None
    cost: float | None = None
    context: int | None = None
    tools: tuple[str, ...] | None = None
    write: bool | None = None
    security: bool = False
    review: bool = False
    provider: str = "unknown"
    provider_label: str = "unknown"
    tier: str | None = None
    alias_for: str | None = None
    require_availability_signal: bool = False
    credential_env: str | None = None
    risk_ceiling: str | None = None
    fallback: tuple[str, ...] = ()
    reasoning_effort_issue: str | None = None
    risk_ceiling_issue: str | None = None
    capabilities: Capabilities = Capabilities()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "autonomy": self.autonomy,
            "capability_mode": self.capability_mode,
            "description": self.description,
            "source": self.source,
            "task": list(self.task) if self.task is not None else None,
            "support": self.support,
            "preference": self.preference,
            "quality_prior": self.quality_prior,
            "latency": self.latency,
            "quota": self.quota,
            "cost": self.cost,
            "context": self.context,
            "tools": list(self.tools) if self.tools is not None else None,
            "write": self.write,
            "security": self.security,
            "review": self.review,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "tier": self.tier,
            "alias_for": self.alias_for,
            "require_availability_signal": self.require_availability_signal,
            "risk_ceiling": self.risk_ceiling,
            "fallback": list(self.fallback),
            "capabilities": self.capabilities.to_dict(),
        }


class RoleRegistry:
    def __init__(
        self,
        roles: Mapping[str, Role],
        known_models: frozenset[str] = frozenset(),
        warnings: tuple[str, ...] = (),
        provider_catalog: Mapping[str, ProviderModelMeta] | None = None,
        executable_bindings: Mapping[str, ExecutableModelBinding] | None = None,
    ) -> None:
        self._roles: dict[str, Role] = {str(k): v for k, v in roles.items()}
        self.known_models = frozenset(known_models)
        self.warnings = tuple(warnings)
        self.provider_catalog = dict(provider_catalog or {})
        self.executable_bindings = dict(executable_bindings or {})

    def with_provider_catalog(
        self, provider_catalog: Mapping[str, ProviderModelMeta]
    ) -> "RoleRegistry":
        """Return an equivalent registry using the supplied provider catalog."""
        return RoleRegistry(
            self._roles,
            self.known_models,
            self.warnings,
            provider_catalog=provider_catalog,
            executable_bindings=self.executable_bindings,
        )

    def get(self, name: str | None) -> Role | None:
        if not name:
            return None
        return self._roles.get(name)

    def canonical(self, name: str | None) -> str | None:
        """Resolve a public alias to its canonical role name (identity if none)."""
        if not name:
            return None
        role = self._roles.get(name)
        if role is not None and role.alias_for:
            return role.alias_for
        return ROLE_ALIASES.get(name, name)

    def canonical_role(self, name: str | None) -> Role | None:
        canonical = self.canonical(name)
        return self._roles.get(canonical) if canonical else None

    def names(self) -> frozenset[str]:
        return frozenset(self._roles)

    def as_dict(self) -> dict[str, Any]:
        return {
            "roles": {name: role.to_dict() for name, role in sorted(self._roles.items())},
            "known_models": sorted(self.known_models),
            "aliases": dict(sorted(ROLE_ALIASES.items())),
            "warnings": list(self.warnings),
        }

    def validate_roles(self) -> list[dict[str, str]]:
        """Validate the registry itself: read-only invariants, alias targets, model catalog."""
        issues: list[dict[str, str]] = []
        model_catalog = set(self.known_models) | set(self.provider_catalog)
        for alias, canonical in sorted(ROLE_ALIASES.items()):
            alias_role = self.get(alias)
            canonical_role = self.get(canonical)
            if canonical_role is None:
                issues.append(
                    {
                        "level": "error",
                        "role": alias,
                        "msg": f"canonical alias target '{canonical}' is not a declared role",
                    }
                )
            elif alias_role is not None and (
                alias_role.model,
                alias_role.reasoning_effort,
                alias_role.autonomy,
            ) != (
                canonical_role.model,
                canonical_role.reasoning_effort,
                canonical_role.autonomy,
            ):
                issues.append(
                    {
                        "level": "warning" if alias == "review" else "error",
                        "role": alias,
                        "msg": f"alias pair ({alias_role.model}, {alias_role.reasoning_effort}) != canonical '{canonical}' pair ({canonical_role.model}, {canonical_role.reasoning_effort})",
                    }
                )
        for name, role in sorted(self._roles.items()):
            if role.autonomy not in VALID_AUTONOMY:
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"autonomy must be one of: {', '.join(sorted(VALID_AUTONOMY))}",
                    }
                )
            if role.reasoning_effort_issue:
                issues.append(
                    {"level": "error", "role": str(name), "msg": role.reasoning_effort_issue}
                )
            if role.risk_ceiling_issue:
                issues.append({"level": "error", "role": str(name), "msg": role.risk_ceiling_issue})
            if (
                role.reasoning_effort is not None
                and role.reasoning_effort not in CANONICAL_REASONING_EFFORTS
            ):
                issues.append(
                    {
                        "level": "warning",
                        "role": str(name),
                        "msg": f"noncanonical reasoning effort '{role.reasoning_effort}' retained",
                    }
                )
            for fallback in role.fallback:
                if self.get(fallback) is None:
                    issues.append(
                        {
                            "level": "error",
                            "role": str(name),
                            "msg": f"fallback target '{fallback}' is not a declared role",
                        }
                    )
            if (
                role.name in {"visual-intake", "visual-intake-deep"}
                and role.model
                and role.capabilities.vision is not True
            ):
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"visual role '{name}' requires explicit catalog capabilities.vision=true",
                    }
                )
            if role.alias_for and self.get(role.alias_for) is None:
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"alias target '{role.alias_for}' is not a declared role",
                    }
                )
            if role.name in READ_ONLY_CANONICAL and role.write is True:
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"role '{name}' must be read-only (analysis/verification only)",
                    }
                )
            if role.name in IMPLEMENT_ROLE_NAMES and role.write is not True:
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"implement role '{name}' must be writable (default_capability_mode=all)",
                    }
                )
            if role.review and role.name not in READ_ONLY_CANONICAL and role.write is not False:
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": f"review role '{name}' must be read-only",
                    }
                )
            provider = self.provider_catalog.get(role.model) if role.model else None
            if provider is not None and provider.tier == "tryout":
                issues.append(
                    {
                        "level": "error",
                        "role": str(name),
                        "msg": "tryout models are never role-routable",
                    }
                )
            if role.model and model_catalog and role.model not in model_catalog:
                issues.append(
                    {
                        "level": "warning",
                        "role": str(name),
                        "msg": f"role '{name}' model '{role.model}' is not in config [model.*] nor providers.json",
                    }
                )
        return issues

    def validate_intents(self, intents_spec: Mapping[str, Any]) -> list[dict[str, str]]:
        """Return a list of {level, intent, msg} issues for the intent table."""

        issues: list[dict[str, str]] = []
        intents = intents_spec.get("intents", {}) if isinstance(intents_spec, Mapping) else {}
        for name, intent in intents.items():
            role_name = str(intent.get("role") or name)
            canonical = self.canonical(role_name)
            role = self.get(canonical) if canonical else None
            if role is None:
                issues.append(
                    {
                        "level": "error",
                        "intent": str(name),
                        "msg": f"role '{role_name}' is not a spawnable agent type",
                    }
                )
                continue
            model = intent.get("model")
            if model and role.model and model != role.model:
                issues.append(
                    {
                        "level": "warning",
                        "intent": str(name),
                        "msg": f"intent model '{model}' != role '{role_name}' model '{role.model}' (config wins)",
                    }
                )
            if role.model and self.known_models and role.model not in self.known_models:
                issues.append(
                    {
                        "level": "warning",
                        "intent": str(name),
                        "msg": f"role '{role_name}' model '{role.model}' not declared in config [model.*]",
                    }
                )
        return issues


def _grok_home() -> Path:
    configured = os.environ.get("GROK_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".grok"


def default_config_path() -> Path:
    return _grok_home() / "config.toml"


def resolve_config_path(config_path: Path | str | None = None) -> Path | None:
    if config_path is not None:
        candidate = Path(config_path)
        return candidate if candidate.is_file() else None
    # The hook must read the *effective live* config Grok actually uses when one
    # exists (symlink or regular file), never fall back to the repo copy at hook
    # time and pretend a role is spawnable that the live config does not define.
    # The repo copy is only a last resort for a not-yet-installed checkout (and
    # for hermetic tests that point GROK_HOME at an empty temp dir).
    for candidate in (
        default_config_path(),
        repo_config_path(),
    ):
        if candidate.is_file():
            return candidate
    return None


def _role_from(
    name: str,
    model: str | None,
    capability_mode: str,
    description: str,
    source: str,
    spec: Mapping[str, Any] | None,
    model_info: Mapping[str, Any] | None,
    provider_meta: ProviderModelMeta | None,
) -> Role:
    spec = spec or {}
    model_info = model_info or {}
    task = TASK_BY_ROLE_NAME.get(name)
    if spec.get("task") is not None:
        task = tuple(str(x) for x in spec.get("task"))
    write = None
    if capability_mode in {"read-only"}:
        write = False
    elif capability_mode in {"all", "read-write"}:
        write = True
    if spec.get("write") is not None:
        write = bool(spec.get("write"))
    provider = "unknown"
    provider_label = "unknown"
    if provider_meta is not None:
        provider = provider_meta.provider
        provider_label = provider_meta.provider_label
    elif model_info.get("base_url"):
        provider = provider_from(model_info.get("base_url"))
    if spec.get("provider"):
        provider = str(spec.get("provider"))
    if spec.get("provider_label"):
        provider_label = str(spec.get("provider_label"))
    context = None
    if model_info.get("context_window") is not None:
        context = int(model_info.get("context_window"))
    if spec.get("context") is not None:
        context = int(spec.get("context"))

    _provider_float_keys = {"cost", "latency", "quality_prior"}

    def _opt_float(key: str) -> float | None:
        value = spec.get(key)
        if value is not None:
            return float(value)
        if provider_meta is not None and key in _provider_float_keys:
            return getattr(provider_meta, key)
        return None

    def _opt_int(key: str) -> int | None:
        value = spec.get(key)
        return None if value is None else int(value)

    fallback = tuple(str(x) for x in (spec.get("fallback") or []))
    # Config `alias` is intentionally ignored: ROLE_ALIASES is the single
    # canonical alias table. A stale live config that still carries an `alias`
    # key must never be able to redirect `review`/`implement` away from the
    # router's canonical mapping.
    alias_for = None
    require_signal = bool(
        spec.get(
            "require_availability_signal",
            provider_meta.require_availability_signal if provider_meta else False,
        )
    )
    tier = (
        str(spec.get("tier"))
        if spec.get("tier") is not None
        else (provider_meta.tier if provider_meta else None)
    )
    effort_raw = spec.get("reasoning_effort")
    effort: str | None = None
    effort_issue: str | None = None
    if effort_raw is not None:
        if isinstance(effort_raw, str) and effort_raw:
            effort = effort_raw
        else:
            effort_issue = "reasoning_effort must be a non-empty canonical string"
    ceiling_raw = spec.get("risk_ceiling")
    risk_ceiling = ceiling_raw if isinstance(ceiling_raw, str) else None
    risk_ceiling_issue = None
    if ceiling_raw is not None and risk_ceiling not in VALID_RISK_CEILINGS:
        risk_ceiling = None
        risk_ceiling_issue = "risk_ceiling must be one of: low, medium, high"

    autonomy = spec.get("autonomy", "standard")
    return Role(
        name=name,
        model=str(model) if model else None,
        reasoning_effort=effort,
        autonomy=str(autonomy),
        capability_mode=capability_mode,
        description=description,
        source=source,
        task=task,
        support=str(spec.get("support")) if spec.get("support") is not None else None,
        preference=_opt_int("preference"),
        quality_prior=_opt_float("quality_prior"),
        latency=_opt_float("latency"),
        quota=_opt_float("quota"),
        cost=_opt_float("cost"),
        context=context,
        tools=tuple(str(x) for x in (spec.get("tools") or []))
        if spec.get("tools") is not None
        else None,
        write=write,
        security=bool(spec.get("security", name in {"security", "security-verify"})),
        review=bool(
            spec.get(
                "review", name in {"review", "review-hard", "review-independent", "expert-rescue"}
            )
        ),
        provider=provider,
        provider_label=provider_label,
        tier=tier,
        alias_for=alias_for,
        require_availability_signal=require_signal,
        credential_env=(
            str(spec.get("credential_env") or model_info.get("env_key"))
            if spec.get("credential_env") or model_info.get("env_key")
            else (provider_meta.credential_env if provider_meta else None)
        ),
        risk_ceiling=risk_ceiling,
        fallback=fallback,
        reasoning_effort_issue=effort_issue,
        risk_ceiling_issue=risk_ceiling_issue,
        capabilities=(
            provider_meta.capabilities
            if provider_meta
            else Capabilities.from_dict(model_info.get("capabilities"))
        ),
    )


def _parse_agent_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse a Grok agent ``.md`` frontmatter into a small dict, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    out: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().casefold()] = value.strip().strip("\"'")
    return out


def discover_agent_files(grok_home: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{agent_name: frontmatter}`` for user-scope agent files.

    Scans only ``GROK_HOME/agents`` — the location the installer manages. The
    hook must validate the *installed* catalog on this account, never the
    project checkout next to ``roles.py`` (that would claim a role is spawnable
    even when the live account catalog cannot discover it).
    """
    if grok_home is None:
        grok_home = _grok_home()
    else:
        grok_home = Path(grok_home)
    directory = grok_home / "agents"
    found: dict[str, dict[str, Any]] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            meta = _parse_agent_frontmatter(path)
            if not meta:
                continue
            name = str(meta.get("name") or path.stem)
            if name and name not in found:
                found[name] = meta
    return found


def _agent_role(
    name: str, meta: dict[str, Any], provider_catalog: Mapping[str, ProviderModelMeta]
) -> Role:
    """Build a ``Role`` from an agent ``.md`` frontmatter.

    ``permission_mode`` "plan" (or "read-only") is Grok's read-only agent mode;
    anything else is treated as full capability. Security/review flags are
    still derived from the role name so pipeline validation invariants hold.
    """
    model = str(meta.get("model")) if meta.get("model") else None
    effort_raw = meta.get("reasoning_effort")
    effort = effort_raw if isinstance(effort_raw, str) and effort_raw else None
    effort_issue = (
        None
        if effort_raw is None or effort is not None
        else "reasoning_effort must be a non-empty canonical string"
    )
    capability = (
        "read-only"
        if str(meta.get("permission_mode") or "").casefold() in {"plan", "read-only"}
        else "all"
    )
    meta_obj = provider_catalog.get(model) if model else None
    return Role(
        name=name,
        model=model,
        reasoning_effort=effort,
        autonomy=str(meta.get("autonomy") or "standard"),
        capability_mode=capability,
        description=str(meta.get("description") or ""),
        source="agent",
        task=TASK_BY_ROLE_NAME.get(name),
        write=capability in {"all", "read-write"},
        security=(name in {"security", "security-verify"}),
        review=(name in {"review", "review-hard", "review-independent", "expert-rescue"}),
        provider=meta_obj.provider if meta_obj else "unknown",
        provider_label=meta_obj.provider_label if meta_obj else "unknown",
        tier=meta_obj.tier if meta_obj else None,
        require_availability_signal=bool(meta_obj.require_availability_signal)
        if meta_obj
        else False,
        reasoning_effort_issue=effort_issue,
        capabilities=meta_obj.capabilities if meta_obj else Capabilities(),
    )


def load_registry(config_path: Path | str | None = None) -> RoleRegistry:
    roles: dict[str, Role] = {}
    provider_catalog = load_provider_catalog()
    for name, spec in BUILTIN_ROLES.items():
        roles[name] = Role(
            name=name,
            model=spec.get("model"),
            reasoning_effort=spec.get("reasoning_effort"),
            autonomy=str(spec.get("autonomy", "standard")),
            capability_mode=str(spec.get("capability_mode", "all")),
            description=str(spec.get("description", "")),
            source="builtin",
            task=TASK_BY_ROLE_NAME.get(name),
            write=False if spec.get("capability_mode") == "read-only" else None,
            security=(name == "security"),
            review=(name == "review"),
            capabilities=(
                provider_catalog.get(spec.get("model")).capabilities
                if provider_catalog.get(spec.get("model"))
                else Capabilities()
            ),
        )

    warnings: list[str] = []
    known_models: set[str] = set()
    path = resolve_config_path(config_path)
    if path is None:
        warnings.append("config.toml not found; using built-in roles only")
        return RoleRegistry(roles, frozenset(), tuple(warnings), provider_catalog)

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"config.toml unreadable ({exc}); using built-in roles only")
        return RoleRegistry(roles, frozenset(), tuple(warnings), provider_catalog)

    model_blocks = data.get("model", {}) if isinstance(data, Mapping) else {}
    executable_bindings: dict[str, ExecutableModelBinding] = {}
    if isinstance(model_blocks, Mapping):
        known_models.update(str(k) for k in model_blocks.keys())
        for key, model_spec in model_blocks.items():
            if not isinstance(model_spec, Mapping):
                continue
            executable_bindings[str(key)] = ExecutableModelBinding(
                model=str(key),
                base_url=(str(model_spec.get("base_url")) if model_spec.get("base_url") else None),
                credential_env=(
                    str(model_spec.get("env_key")) if model_spec.get("env_key") else None
                ),
            )

    subagents = data.get("subagents", {}) if isinstance(data, Mapping) else {}
    if isinstance(subagents, Mapping):
        model_overrides = subagents.get("models", {})
        if isinstance(model_overrides, Mapping):
            for name, model in model_overrides.items():
                if name in roles:
                    roles[name] = Role(
                        name=str(name),
                        model=str(model) if model else None,
                        reasoning_effort=roles[name].reasoning_effort,
                        autonomy=roles[name].autonomy,
                        capability_mode=roles[name].capability_mode,
                        description=roles[name].description,
                        source="config.models",
                        task=roles[name].task,
                        write=roles[name].write,
                        security=roles[name].security,
                        review=roles[name].review,
                        provider=provider_catalog.get(str(model)).provider
                        if model and provider_catalog.get(str(model))
                        else "unknown",
                        provider_label=provider_catalog.get(str(model)).provider_label
                        if model and provider_catalog.get(str(model))
                        else "unknown",
                        tier=provider_catalog.get(str(model)).tier
                        if model and provider_catalog.get(str(model))
                        else None,
                        require_availability_signal=provider_catalog.get(
                            str(model)
                        ).require_availability_signal
                        if model and provider_catalog.get(str(model))
                        else False,
                        risk_ceiling=roles[name].risk_ceiling,
                        capabilities=provider_catalog.get(str(model)).capabilities
                        if model and provider_catalog.get(str(model))
                        else Capabilities(),
                    )
        inline_roles = subagents.get("roles", {})
        if isinstance(inline_roles, Mapping):
            for name, spec in inline_roles.items():
                if not isinstance(spec, Mapping):
                    continue
                model = spec.get("model")
                model_key = str(model) if model else ""
                model_info = (
                    model_blocks.get(model_key, {}) if isinstance(model_blocks, Mapping) else {}
                )
                roles[str(name)] = _role_from(
                    str(name),
                    model,
                    str(spec.get("default_capability_mode", "all")),
                    str(spec.get("description", "")),
                    "config.roles",
                    spec,
                    model_info if isinstance(model_info, Mapping) else {},
                    provider_catalog.get(model_key),
                )

    # Grok discovers spawnable agent *types* from `.grok/agents/*.md`, not from
    # `[subagents.roles.*]`. Layer agent files in as an additional source so a
    # stale live config that is missing a role (e.g. implement-cheap-fallback /
    # review-hard) can still resolve that role from its agent definition.
    # Inline config roles take precedence over agent files.
    for agent_name, meta in discover_agent_files().items():
        if agent_name in roles:
            continue
        roles[agent_name] = _agent_role(agent_name, meta, provider_catalog)

    review_alias = roles.get("review")
    review_canonical = roles.get("review-hard")
    if (
        review_alias
        and review_canonical
        and (
            review_alias.model,
            review_alias.reasoning_effort,
            review_alias.autonomy,
        )
        != (
            review_canonical.model,
            review_canonical.reasoning_effort,
            review_canonical.autonomy,
        )
    ):
        warnings.append("role 'review' is a legacy alias; prefer review-hard")
    for role in roles.values():
        if (
            role.reasoning_effort is not None
            and role.reasoning_effort not in CANONICAL_REASONING_EFFORTS
        ):
            warnings.append(
                f"role '{role.name}': noncanonical reasoning effort '{role.reasoning_effort}' retained"
            )
    return RoleRegistry(
        roles,
        frozenset(known_models),
        tuple(warnings),
        provider_catalog,
        executable_bindings,
    )


__all__ = [
    "Capabilities",
    "ExecutableModelBinding",
    "ProviderEndpointMeta",
    "ProviderModelMeta",
    "Role",
    "RoleRegistry",
    "ProviderModelMeta",
    "load_registry",
    "load_provider_catalog",
    "resolve_config_path",
    "default_config_path",
    "BUILTIN_ROLES",
    "ROLE_ALIASES",
    "READ_ONLY_CANONICAL",
    "VISUAL_INTAKE_ROLES",
    "RECON_ROLES",
    "EXECUTION_READ_ONLY_ROLES",
    "IMPLEMENT_ROLE_NAMES",
    "family_of",
    "provider_from",
    "role_credential_present",
    "CANONICAL_REASONING_EFFORTS",
    "VALID_AUTONOMY",
]
