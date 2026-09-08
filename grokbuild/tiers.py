"""Data-derived provider subscription tier helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .roles import credential_present, load_provider_catalog

PACKAGE_ROOT = Path(__file__).resolve().parent
TIER_PROVIDER_SUBSETS: dict[str, frozenset[str]] = {
    "Minimum": frozenset({"codex"}),
    "Recommended": frozenset({"codex", "zai", "opencode"}),
    "Full": frozenset({"codex", "zai", "opencode", "xai", "commandcode", "minimax", "together"}),
}


def load_role_data(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else PACKAGE_ROOT / "roles_default.json"
    return json.loads(target.read_text(encoding="utf-8"))


def load_provider_data(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else PACKAGE_ROOT / "providers.json"
    return json.loads(target.read_text(encoding="utf-8"))


def role_provider_map(
    providers: Mapping[str, Any] | None = None,
    roles: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Map every configured role to its provider using the two JSON sources."""
    provider_data = providers if providers is not None else load_provider_data()
    role_data = roles if roles is not None else load_role_data()
    models: dict[str, str] = {}
    for provider, spec in provider_data.get("providers", {}).items():
        raw_models = spec.get("models", {}) if isinstance(spec, Mapping) else {}
        for model, model_spec in raw_models.items():
            endpoints = model_spec.get("endpoints") if isinstance(model_spec, Mapping) else None
            primary = endpoints[0] if isinstance(endpoints, list) and endpoints else None
            models[str(model)] = str(
                primary.get("provider", provider) if isinstance(primary, Mapping) else provider
            )
    return {
        str(role): models[str(spec["model"])]
        for role, spec in role_data.items()
        if isinstance(spec, Mapping) and spec.get("model") in models
    }


def roles_for_providers(
    providers: set[str] | frozenset[str],
    mapping: Mapping[str, str] | None = None,
) -> frozenset[str]:
    role_map = mapping if mapping is not None else role_provider_map()
    return frozenset(role for role, provider in role_map.items() if provider in providers)


def provider_credentials(
    env: Mapping[str, str],
    home: str | None = None,
) -> dict[str, bool]:
    """Return credential presence by provider, without returning credential values."""
    catalog = load_provider_catalog()
    providers = load_provider_data().get("providers", {})
    check_env = dict(env)
    if home is not None:
        check_env["HOME"] = home
    result: dict[str, bool] = {}
    for provider, spec in providers.items():
        models = spec.get("models", {}) if isinstance(spec, Mapping) else {}
        model = next(iter(models), None)
        meta = catalog.get(model) if model is not None else None
        result[str(provider)] = bool(model is not None and credential_present(meta, check_env))
    return result


def resolved_roles(
    credentials: Mapping[str, bool],
    mapping: Mapping[str, str] | None = None,
    availability_signals: set[str] | frozenset[str] = frozenset(),
) -> frozenset[str]:
    role_map = mapping if mapping is not None else role_provider_map()
    data = load_provider_data()
    return frozenset(
        role
        for role, provider in role_map.items()
        if credentials.get(provider, False)
        and (
            not bool(data.get("providers", {}).get(provider, {}).get("require_availability_signal"))
            or provider in availability_signals
        )
    )


def achieved_level(
    credentials: Mapping[str, bool],
    mapping: Mapping[str, str] | None = None,
    availability_signals: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, frozenset[str]]:
    role_map = mapping if mapping is not None else role_provider_map()
    resolved = resolved_roles(credentials, role_map, availability_signals)
    for level in ("Full", "Recommended", "Minimum"):
        providers = TIER_PROVIDER_SUBSETS[level]
        required = roles_for_providers(providers, role_map)
        if (
            providers <= {provider for provider, present in credentials.items() if present}
            and required <= resolved
        ):
            return level, frozenset()
    minimum = roles_for_providers(TIER_PROVIDER_SUBSETS["Minimum"], role_map)
    missing = minimum - resolved
    return "Observe-only", missing
