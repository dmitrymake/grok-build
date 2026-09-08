#!/usr/bin/env python3
"""Render the checked-in Grok Build contract projections."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from grokbuild.conductor import CONDUCTOR_CANDIDATES
from grokbuild.roles import load_registry
from ._repo import repo_root

SCHEMA = 1
ROOT = repo_root()
PACKAGE_ROOT = Path(__file__).resolve().parent
TARGETS = {
    "agents-contract": ROOT / "AGENTS.md",
    "security-contract": ROOT / "rules/security-models.md",
    "routing-contract": ROOT / "grokbuild/README.md",
    "runbook-contract": ROOT / "docs/grok-build.md",
}
BEGIN = "<!-- grok-build:generated begin id={id} schema=1 -->"
END = "<!-- grok-build:generated end id={id} schema=1 -->"
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_json(path: Path) -> Any:
    """Load JSON from a UTF-8 path."""
    return json.loads(path.read_text(encoding="utf-8"))


def fail(message: str) -> None:
    """Raise a validation error with the supplied message."""
    if message.startswith("SOURCE_MISMATCH"):
        return
    raise ValueError(message)


def parse_providers(path: Path) -> dict[str, dict[str, Any]]:
    """Parse and validate the provider catalog into model-indexed records."""
    data = load_json(path)
    if not isinstance(data, dict) or set(data) != {"version", "note", "providers"}:
        fail("providers.json has unexpected top-level keys")
    if data["version"] != 2 or not isinstance(data["providers"], dict):
        fail("providers.json schema is invalid")
    out: dict[str, dict[str, Any]] = {}
    provider_keys = {
        "label",
        "subscription_class",
        "auth",
        "credential_env",
        "credential_path",
        "quota_probe",
        "require_availability_signal",
        "models",
    }
    model_keys = {
        "family",
        "tier",
        "cost",
        "latency",
        "quality_prior",
        "capabilities",
        "balance",
    }
    endpoint_keys = {
        "provider",
        "model_binding",
        "subscription_class",
        "auth",
        "credential_env",
        "credential_path",
        "require_availability_signal",
        "availability_key",
    }
    capability_keys = {"text", "vision", "audio", "video", "tools", "structured_output", "logprobs"}
    for provider, spec in data["providers"].items():
        if (
            not isinstance(spec, dict)
            or set(spec) - provider_keys
            or not isinstance(spec.get("models"), dict)
        ):
            fail(f"provider {provider} is invalid")
        for key in ("label", "subscription_class", "auth", "require_availability_signal", "models"):
            if key not in spec:
                fail(f"provider {provider} missing {key}")
        if spec["subscription_class"] not in {"primary", "reserve"}:
            fail(f"provider {provider} has invalid subscription_class")
        quota_probe = spec.get("quota_probe")
        if quota_probe is not None and quota_probe not in {"opencode", "codex"}:
            fail(f"provider {provider} has invalid quota_probe")
        env = spec.get("credential_env")
        if env is not None and (not isinstance(env, str) or not ENV_RE.fullmatch(env)):
            fail(f"provider {provider} has invalid credential_env")
        for model, mspec in spec["models"].items():
            if (
                not isinstance(mspec, dict)
                or set(mspec) - (model_keys | {"description", "endpoints"})
                or not model_keys - {"balance"} <= set(mspec)
                or set(mspec.get("capabilities", {})) != capability_keys
            ):
                fail(f"provider model {model} is invalid")
            balance = mspec.get("balance")
            if balance is not None and balance not in {"ordered", "rotate"}:
                fail(f"provider model {model} has invalid balance")
            endpoints = mspec.get("endpoints")
            if endpoints is not None:
                if not isinstance(endpoints, list) or not endpoints:
                    fail(f"provider model {model} endpoints are invalid")
                for endpoint in endpoints:
                    if (
                        not isinstance(endpoint, dict)
                        or set(endpoint) - endpoint_keys
                        or not {"provider", "subscription_class"} <= set(endpoint)
                        or endpoint["provider"] not in data["providers"]
                        or endpoint["subscription_class"] not in {"primary", "reserve"}
                    ):
                        fail(f"provider model {model} endpoint is invalid")
                    model_binding = endpoint.get("model_binding")
                    if model_binding is not None and (
                        not isinstance(model_binding, str) or not model_binding
                    ):
                        fail(f"provider model {model} endpoint model binding is invalid")
                    endpoint_env = endpoint.get("credential_env")
                    if endpoint_env is not None and (
                        not isinstance(endpoint_env, str) or not ENV_RE.fullmatch(endpoint_env)
                    ):
                        fail(f"provider model {model} endpoint credential is invalid")
            primary = endpoints[0] if endpoints else {}
            projected_provider = primary.get("provider", provider)
            projected_spec = data["providers"][projected_provider]
            out[model] = {
                "provider": projected_provider,
                **spec,
                **projected_spec,
                **mspec,
                **primary,
            }
    return out


def marker_blocks(text: str) -> dict[str, tuple[int, int]]:
    """Locate and validate generated documentation marker blocks."""
    pattern = re.compile(r"<!-- grok-build:generated (begin|end) id=([^ ]+) schema=([^ ]+) -->")
    active: dict[str, int] = {}
    found: dict[str, tuple[int, int]] = {}
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line.rstrip("\n"))
        if not match:
            continue
        kind, ident, schema = match.groups()
        if schema != "1":
            fail(f"marker schema is not 1 in {ident}")
        if kind == "begin":
            if ident in active or ident in found:
                fail(f"duplicate or nested marker {ident}")
            active[ident] = index
        else:
            if ident not in active:
                fail(f"end without begin for {ident}")
            found[ident] = (active.pop(ident), index)
    if active:
        fail(f"unterminated marker {next(iter(active))}")
    return found


def model_spec(config: dict[str, Any], model: str) -> dict[str, Any]:
    """Return a configured model specification."""
    blocks = config.get("model", {})
    if model == "grok-4.6" and model not in blocks:
        return {}
    if model not in blocks or not isinstance(blocks[model], dict):
        fail(f"model {model} is absent from config")
    return blocks[model]


def verifier_table(config: dict[str, Any]) -> dict[str, list[str]]:
    """Return the configured repository verifier table."""
    routing = config.get("routing", {})
    if not isinstance(routing, dict):
        fail("verifiers table malformed")
    verifiers = routing.get("verifiers", {})
    if not isinstance(verifiers, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, list)
        or not value
        or any(not isinstance(command, str) or not command.strip() for command in value)
        for key, value in verifiers.items()
    ):
        fail("verifiers table malformed")
    return verifiers


def repo_verifiers(config: dict[str, Any]) -> list[str]:
    """Return verifier commands configured for the repository root."""
    verifiers = verifier_table(config)
    commands = verifiers.get("$REPO_ROOT")
    if commands is None:
        fail("verifiers table malformed")
    return commands


def validate(
    config: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    intents: dict[str, Any],
    prose: dict[str, Any],
    registry: Any,
    conductor_candidates: tuple[str, ...],
) -> None:
    """Validate configuration, provider, registry, prose, and conductor invariants."""
    if prose.get("schema_version") != 1:
        fail("contract_prose schema_version must be 1")
    roles = config.get("subagents", {}).get("roles", {})
    expected = set(roles) | {"general-purpose"}
    if set(prose.get("role_contracts", {})) != expected:
        fail("role_contracts must match config roles plus general-purpose")
    for role, value in prose["role_contracts"].items():
        if (
            not isinstance(value, dict)
            or set(value) != {"en"}
            or not all(isinstance(value[k], str) and value[k] for k in value)
        ):
            fail(f"role_contracts.{role} is invalid")
    registry_issues = registry.validate_roles()
    registry_errors = [issue for issue in registry_issues if issue.get("level") == "error"]
    for issue in registry_issues:
        if issue.get("level") == "warning":
            print(f"render_docs: warning: {issue.get('msg', 'unknown issue')}")
    if registry_errors:
        fail("role registry validation failed: " + registry_errors[0].get("msg", "unknown issue"))
    if config.get("models", {}).get("default") != conductor_candidates[0]:
        fail("config default does not match conductor primary")
    if config.get("models", {}).get("web_search") != prose["notes"]["web_research"]["model"]:
        fail("web search model mismatch")
    config_models = set(config.get("model", {}))
    known = config_models | set(providers)
    bindings = prose["binding_rules"]
    provider_pool = bindings["provider_pool_a"]
    pool_spec = model_spec(config, provider_pool["binding"])
    expected_pool_roles = {
        role for role, spec in roles.items() if spec.get("model") == provider_pool["binding"]
    }
    if set(provider_pool["roles"]) != expected_pool_roles:
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.roles")
    for role in provider_pool["roles"]:
        if roles.get(role, {}).get("model") != provider_pool["binding"]:
            fail(f"SOURCE_MISMATCH binding_rules.provider_pool_a.roles.{role}")
    pool_provider = providers.get(provider_pool["binding"])
    if not pool_provider or pool_provider["provider"] != provider_pool["provider"]:
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.provider")
    if provider_pool["credential_env_pattern"] != "ZAI_API_KEY":
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.credential_env_pattern")
    for model in provider_pool["models"]:
        if (
            model not in config_models
            or providers.get(model, {}).get("provider") != provider_pool["provider"]
        ):
            fail(f"SOURCE_MISMATCH binding_rules.provider_pool_a.models.{model}")
        if model_spec(config, model).get("context_window") != provider_pool["declared_context"]:
            fail(f"SOURCE_MISMATCH binding_rules.provider_pool_a.context.{model}")
    if pool_spec.get("model") != provider_pool["raw_model"]:
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.raw_model")
    if pool_provider["provider"] not in {spec.get("provider") for spec in providers.values()}:
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.provider")
    if pool_spec.get("context_window") != provider_pool["declared_context"]:
        fail("SOURCE_MISMATCH binding_rules.provider_pool_a.declared_context")

    grok = bindings["grok_session_auth"]
    grok_provider = providers.get(grok["model"])
    if (
        not grok_provider
        or grok_provider["provider"] != grok["provider"]
        or grok_provider.get("auth") != grok["auth"]
    ):
        fail("SOURCE_MISMATCH binding_rules.grok_session_auth.provider/auth")
    expected_grok_roles = {
        role for role, spec in roles.items() if spec.get("model") == grok["model"]
    }
    if set(grok["roles"]) != expected_grok_roles:
        fail("SOURCE_MISMATCH binding_rules.grok_session_auth.roles")
    for role in grok["roles"]:
        if roles.get(role, {}).get("model") != grok["model"]:
            fail(f"SOURCE_MISMATCH binding_rules.grok_session_auth.roles.{role}")
    if grok["model"] in config_models:
        fail("SOURCE_MISMATCH binding_rules.grok_session_auth.model_config_absence")

    model_proxy = bindings["model_proxy"]
    if model_proxy["provider"] not in {spec.get("provider") for spec in providers.values()}:
        fail("SOURCE_MISMATCH binding_rules.model_proxy.provider")
    if model_proxy["auth"] != "configured proxy endpoint and API key":
        fail("SOURCE_MISMATCH binding_rules.model_proxy.auth")
    for model in model_proxy["models"]:
        if model not in config_models or model not in providers:
            fail(f"SOURCE_MISMATCH binding_rules.model_proxy.models.{model}")
    batch = prose["notes"]["batch_pool"]
    for model in batch["models"]:
        if model not in config_models or model not in providers:
            fail(f"SOURCE_MISMATCH notes.batch_pool.models.{model}")
    second_opinion = config.get("routing", {}).get("second_opinion", {})
    advisory_model = prose["notes"]["conductor_policy"]["advisory_model"]
    configured_advisory = (
        second_opinion.get("model", "gemini-3.7-flash")
        if isinstance(second_opinion, dict)
        else "gemini-3.7-flash"
    )
    if advisory_model != configured_advisory or advisory_model not in conductor_candidates:
        fail("SOURCE_MISMATCH notes.conductor_policy.advisory_model")
    unbound_challengers = {"judge-challenger-agentic", "judge-challenger-structural"}
    for role, spec in roles.items():
        model = spec.get("model")
        if role in unbound_challengers and model is None:
            continue
        if model not in known:
            fail(f"role {role} references unknown model {model}")
        env = model_spec(config, model).get("env_key")
        if model != "grok-4.6" and (not isinstance(env, str) or not ENV_RE.fullmatch(env)):
            fail(f"model {model} has invalid env_key")
    if "grok-4.6" in config_models:
        fail("grok-4.6 must use session auth and not have a model block")
    for model in conductor_candidates:
        if model not in known:
            fail(f"conductor model {model} is unknown")
    verifier_table(config)


def recipe(role: str, spec: dict[str, Any], global_effort: str) -> str:
    """Render a role model recipe with its launch-time autonomy tier."""
    model = str(spec.get("model") or "unbound (disabled)")
    explicit = spec.get("reasoning_effort")
    pin = f"{model} @ {explicit}" if explicit else model
    return f"{pin} [{spec.get('autonomy', 'standard')}]"


def role_rows(
    config: dict[str, Any], prose: dict[str, Any], providers: dict[str, dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """Build sorted role rows for the agents contract."""
    roles = config["subagents"]["roles"]
    default_effort = config["models"]["default_reasoning_effort"]
    rows = []
    for name in sorted(roles):
        spec = roles[name]
        rows.append((name, recipe(name, spec, default_effort), prose["role_contracts"][name]["en"]))
    return rows


def render_agents(
    config: dict[str, Any], prose: dict[str, Any], conductor_candidates: tuple[str, ...]
) -> str:
    """Render the agents contract projection."""
    verifiers = repo_verifiers(config)
    verifier_text = " and ".join(f"`{command}`" for command in verifiers)
    chain = " → ".join(conductor_candidates)
    pool_models = ", ".join(
        f"`{model}`" for model in prose["binding_rules"]["provider_pool_a"]["models"]
    )
    bullets = [
        "Stable job-role names are the public contract; vendor- or model-named roles are forbidden.",
        "Briefing autonomy is launch-time policy: guided roles receive complete precise briefs, standard roles receive goals and constraints with bounded discretion, and broad roles may self-direct read-only investigation within scope.",
        "When a child transcript approaches 80% of its context window, resume or respawn preserving its read-only/writable class; prefer gpt-6-astra (1.05M), then glm-5.3.",
        "Role pins and efforts live only in `config/config.toml`; recipes render as model @ effort. The built-in `general-purpose` role is pinned through `[subagents.models]`.",
        f"The conductor fallback chain is `{chain}`.",
        f"Provider pool A models ({pool_models}) use the configured `ZAI_API_KEY` credential.",
        'The conductor is permanently zero-write; only identified implementation children may write, and they must use `capability_mode="all"`.',
        "Delegated reconnaissance precedes medium/high implementation; the conductor does not map the repository.",
        "Medium/high implementation finishes a review stage or, when review is unavailable, the exact configured deterministic verifier with a warning; without either, the route degrades observe-only.",
        "A failed child spawn, verifier, or bound background job retrieval reopens its stage; re-engage in the same turn or report `BLOCKED` with the failure cause.",
        "A stage failing `consilium_after_failures` consecutive repairs convenes a read-only cross-provider consilium barrier; an implementation tier still applies the fix.",
        "Unfinished execution debt survives turn boundaries within the bounded session window and blocks `end_turn` until repaired or the bounded Stop limit is reached.",
        "Only `kill_command_or_subagent` is a conductor lifecycle exception; barrier stalls remain observe-only.",
        "Media intake roles are semantic, and vision capability must be explicit in the provider catalog; visual preflight is not an intent or execution stage.",
        "Never commit, print, copy, or delete secrets, `~/.env_keys`, or OAuth files.",
        f"For Grok routing changes run {verifier_text}.",
    ]
    return "\n".join(f"- {item}" for item in bullets)


def render_security(
    config: dict[str, Any],
    prose: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    conductor_candidates: tuple[str, ...],
) -> str:
    """Render the security contract projection."""
    default = config["models"]["default_reasoning_effort"]
    chain = " → ".join(conductor_candidates)
    lines = [
        f"The conductor follows `{chain}`, starts at `{conductor_candidates[0]}` with global `{default}` effort, remains zero-write, and uses `{prose['notes']['conductor_policy']['advisory_model']}` for advisory opinions below {prose['notes']['conductor_policy']['advisory_below_confidence']:.1f} confidence. Web research stays on `{prose['notes']['web_research']['model']}`.",
        "",
        "| Job role | Model @ effort | Autonomy | Contract |",
        "| --- | --- | --- | --- |",
    ]
    rows = role_rows(config, prose, providers)
    aliases = {"implement-standard": "implement", "plan-hard": "plan", "review-hard": "review"}
    emitted: set[str] = set()
    for name, pin, contract in rows:
        if name in emitted:
            continue
        pair = [(n, p, c) for n, p, c in rows if n in aliases and aliases[n] == name]
        label = f"`{name}`"
        if pair and pair[0][1] == pin:
            label = (
                f"`{name}` / `{aliases[name]}`" if name in aliases else f"`{name}` / `{pair[0][0]}`"
            )
            emitted.update({name, pair[0][0]})
        else:
            emitted.add(name)
        lines.append(
            f"| {label} | `{pin}` | `{config['subagents']['roles'][name].get('autonomy', 'standard')}` | {contract} |"
        )
    gp = config["subagents"]["models"]["general-purpose"]
    provider_pool = prose["binding_rules"]["provider_pool_a"]
    grok = prose["binding_rules"]["grok_session_auth"]
    model_proxy = prose["binding_rules"]["model_proxy"]
    batch = prose["notes"]["batch_pool"]
    binding = (
        "no model credential binding"
        if not grok["model_credential_binding"]
        else "model credential binding"
    )
    pool_models = ", ".join(f"`{model}`" for model in provider_pool["models"])
    lines += [
        f"| `general-purpose` | `{gp}` | infrastructure-child compatibility pin |",
        "",
        f"Provider pool A supplies {pool_models} via `{provider_pool['credential_env_pattern']}`.",
        f"Grok roles use the official `{grok['login_command']}` session with {binding}.",
        f"Proxy-backed models use a {model_proxy['auth']}.",
        f"Batch models `{batch['models'][0]}` and `{batch['models'][1]}` handle {', '.join(batch['workloads'])} workloads.",
    ]
    return "\n".join(lines)


def tier_lines(config: dict[str, Any]) -> list[str]:
    """Render configured role tier mappings."""
    roles = config["subagents"]["roles"]
    mapping = [
        ("recon", ["explore", "explore-thorough"]),
        ("research", ["researcher", "researcher-analyst", "researcher-challenger"]),
        ("cheap", ["implement-cheap"]),
        ("standard", ["implement-standard", "implement"]),
        ("strong", ["implement-strong"]),
        ("hard", ["implement-hard"]),
        ("ops", ["implement-ops"]),
        ("overflow", ["implement-overflow"]),
        ("fallback", ["implement-cheap-fallback"]),
        ("plan", ["plan-hard", "plan", "planner-strong"]),
        ("review", ["review-hard", "review"]),
        ("consilium", ["consilium-analyst", "consilium-challenger", "consilium-arbiter"]),
        ("security", ["security"]),
        ("visual", ["visual-intake", "visual-intake-deep"]),
    ]
    out = []
    for tier, names in mapping:
        out.append(
            f"- **{tier}:** "
            + ", ".join(
                f"`{n}` = `{recipe(n, roles[n], config['models']['default_reasoning_effort'])}`"
                for n in names
            )
        )
    return out


def render_routing(
    config: dict[str, Any], prose: dict[str, Any], conductor_candidates: tuple[str, ...]
) -> str:
    """Render the routing contract projection."""
    lines = [
        f"The parent chain is `{' → '.join(conductor_candidates)}`; stable role names, not model slugs, are delegated.",
        "",
        "Source ownership: `config/config.toml` owns role/model/effort pins and workspace verifiers; `providers.json` owns provider, tier, auth, and capabilities.",
        "",
        "### Tier mapping",
    ]
    lines += tier_lines(config)
    provider_pool = prose["binding_rules"]["provider_pool_a"]
    lines += [
        "",
        f"Escalation is cheap → standard → hard. Overflow is used only after primary tiers fail. Flash is a strict last resort for low-risk, testable work with a known verifier. Grok roles require an explicit availability signal; provider pool A roles use `{provider_pool['binding']}` with `{provider_pool['credential_env_pattern']}`.",
    ]
    return "\n".join(lines)


def context(value: int | None) -> str:
    """Format a context-window size for documentation."""
    if value == 1000000:
        return "1M"
    if value is None:
        return "session-managed"
    return f"{value:,}".replace(",", " ")


def render_runbook(
    config: dict[str, Any],
    prose: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    conductor_candidates: tuple[str, ...],
) -> str:
    """Render the runbook contract projection."""
    roles = config["subagents"]["roles"]
    conductor_model = config["models"]["default"]
    conductor_spec = model_spec(config, conductor_model)
    conductor_provider = providers[conductor_model]
    grok = prose["binding_rules"]["grok_session_auth"]
    model_proxy = prose["binding_rules"]["model_proxy"]
    conductor_auth = f"{conductor_provider['provider']}; `{conductor_spec.get('base_url', '')}`; {conductor_provider['auth']}; env {conductor_spec.get('env_key')}"
    lines = [
        "Grok Build uses stable job roles with model pins and efforts configured in `config/config.toml`.",
        "",
        f"The non-spawnable conductor follows `{' → '.join(conductor_candidates)}`, starts with `{config['models']['default']} @ {config['models']['default_reasoning_effort']}`, and remains permanently zero-write.",
        "",
        "| Role | Model @ effort | Autonomy | Capability | Model tier | Context | Provider | Subscription class | Endpoint/auth |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- | --- |",
        f"| conductor (not spawnable) | `{config['models']['default']} @ {config['models']['default_reasoning_effort']}` | standard | zero-write | conductor | {context(conductor_spec.get('context_window'))} | {conductor_provider['provider']} | {conductor_provider['subscription_class']} | {conductor_auth} |",
    ]
    for name in sorted(roles):
        spec = roles[name]
        model = spec.get("model")
        ms = model_spec(config, model) if model else {}
        pm = providers.get(model, {}) if model else {}
        auth = pm.get("auth", "config")
        provider = pm.get("provider", "config")
        endpoint = ms.get("base_url", "")
        if provider == model_proxy["provider"]:
            auth = model_proxy["auth"]
        elif model == grok["model"]:
            auth = f"official {grok['login_command']}; {grok['auth']}; {'no binding' if not grok['model_credential_binding'] else 'model binding'}"
        elif ms.get("env_key"):
            auth = f"{auth}; env {ms['env_key']}"
        cap = spec.get("default_capability_mode", "all")
        if pm.get("capabilities", {}).get("vision") is True:
            cap += ", vision"
        lines.append(
            f"| `{name}` | `{recipe(name, spec, config['models']['default_reasoning_effort'])}` | `{spec.get('autonomy', 'standard')}` | {cap} | {pm.get('tier', spec.get('tier', '—'))} | {context(ms.get('context_window'))} | {provider} | {pm.get('subscription_class', '—')} | `{endpoint}`; {auth} |"
        )
    provider_pool = prose["binding_rules"]["provider_pool_a"]
    model_proxy = prose["binding_rules"]["model_proxy"]
    batch = prose["notes"]["batch_pool"]
    lines += [
        "",
        "After `consilium_after_failures` consecutive failures in one repair stage, the harness convenes a read-only, three-provider consilium; an implementation tier applies the arbiter's plan.",
        "",
        f"Provider pool A roles use `{provider_pool['binding']}` via `{provider_pool['credential_env_pattern']}`.",
        f"Proxy-backed models use a {model_proxy['auth']}.",
        f"Web research runs on `{prose['notes']['web_research']['model']}`. Batch models `{batch['models'][0]}` and `{batch['models'][1]}` handle text, extraction, and classification workloads.",
    ]
    return "\n".join(lines)


def projections(
    config: dict[str, Any],
    providers: dict[str, dict[str, Any]],
    intents: dict[str, Any],
    prose: dict[str, Any],
    conductor_candidates: tuple[str, ...],
) -> dict[str, str]:
    """Build all generated contract projections."""
    return {
        "agents-contract": render_agents(config, prose, conductor_candidates),
        "security-contract": render_security(config, prose, providers, conductor_candidates),
        "routing-contract": render_routing(config, prose, conductor_candidates),
        "runbook-contract": render_runbook(config, prose, providers, conductor_candidates),
    }


def replace_block(text: str, ident: str, body: str) -> str:
    """Replace one generated marker block with rendered content."""
    blocks = marker_blocks(text)
    if ident not in blocks:
        fail(f"missing marker {ident}")
    start, end = blocks[ident]
    lines = text.splitlines(keepends=True)
    replacement = (
        BEGIN.format(id=ident) + "\n" + body.rstrip("\n") + "\n" + END.format(id=ident) + "\n"
    )
    return "".join(lines[:start]) + replacement + "".join(lines[end + 1 :])


def render_all(
    paths: dict[str, Path],
    config_path: Path,
    providers_path: Path,
    intents_path: Path,
    prose_path: Path,
) -> dict[Path, str]:
    """Render all target documents from the configured source files."""
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    providers = parse_providers(providers_path)
    intents = load_json(intents_path)
    prose = load_json(prose_path)
    conductor = config.get("routing", {}).get("conductor", {})
    conductor_candidates = (
        tuple(conductor["candidates"])
        if isinstance(conductor, dict)
        and isinstance(conductor.get("candidates"), list)
        and conductor["candidates"]
        else CONDUCTOR_CANDIDATES
    )
    registry = load_registry(config_path)
    validate(config, providers, intents, prose, registry, conductor_candidates)
    expected = projections(config, providers, intents, prose, conductor_candidates)
    result = {}
    for ident, path in paths.items():
        text = path.read_text(encoding="utf-8")
        blocks = marker_blocks(text)
        if set(blocks) - set(paths) or len(blocks) != 1 or ident not in blocks:
            fail(f"marker set invalid in {path}")
        result[path] = replace_block(text, ident, expected[ident])
    return result


def main(argv: list[str] | None = None) -> int:
    """Check or write generated contract documentation."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "write"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    parser.add_argument("--providers", type=Path, default=PACKAGE_ROOT / "providers.json")
    parser.add_argument("--intents", type=Path, default=PACKAGE_ROOT / "intents.json")
    parser.add_argument("--prose", type=Path, default=PACKAGE_ROOT / "contract_prose.json")
    args = parser.parse_args(argv)
    try:
        rendered = render_all(TARGETS, args.config, args.providers, args.intents, args.prose)
        changed = []
        for path, text in rendered.items():
            current = path.read_text(encoding="utf-8")
            if current != text:
                changed.append((path, current, text))
        if args.command == "check":
            if changed:
                path, current, wanted = changed[0]
                old = next(
                    (
                        i
                        for i, (a, b) in enumerate(
                            zip(current.splitlines(), wanted.splitlines()), 1
                        )
                        if a != b
                    ),
                    min(len(current.splitlines()), len(wanted.splitlines())) + 1,
                )
                print(
                    "render_docs: drift: " + ", ".join(str(p) for p, _, _ in changed),
                    file=sys.stderr,
                )
                print(f"first differing line: {old}", file=sys.stderr)
                return 1
            print("render_docs: OK")
            return 0
        if not changed:
            print("render_docs: OK (no changes)")
            return 0
        modes = {p: p.stat().st_mode for p, _, _ in changed}
        for path, _, text in changed:
            fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, modes[path])
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        verify = render_all(TARGETS, args.config, args.providers, args.intents, args.prose)
        if any(path.read_text(encoding="utf-8") != text for path, text in verify.items()):
            fail("post-write verification failed")
        print("render_docs: wrote projections")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"render_docs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
