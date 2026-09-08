#!/usr/bin/env python3
"""Pre-session conductor resolution for the Grok Build launchers.

Chooses the session conductor from the actual fallback chain glm-5.3-flash -> gemini-3.7-flash -> grok-4.6. No secret values are emitted.
"""

from __future__ import annotations

import os
import tomllib
from typing import Any, Mapping

from grokbuild.decision import ConductorHandoff
from grokbuild.roles import credential_present, load_provider_catalog, resolve_config_path
from grokbuild.state import RuntimeState, default_state_path, load_state

_CONDUCTOR_DEFAULT_FALLBACK = "glm-5.3-flash"
_CONDUCTOR_FALLBACK_FALLBACK = "gemini-3.7-flash"
_CONDUCTOR_EMERGENCY_FALLBACK = "grok-4.6"
_CONDUCTOR_CANDIDATES_FALLBACK = (
    _CONDUCTOR_DEFAULT_FALLBACK,
    _CONDUCTOR_FALLBACK_FALLBACK,
    _CONDUCTOR_EMERGENCY_FALLBACK,
)


def _load_conductor_config() -> tuple[str, str, str, tuple[str, ...]]:
    try:
        path = resolve_config_path()
        if path is None:
            return (
                *_CONDUCTOR_CANDIDATES_FALLBACK[:1],
                _CONDUCTOR_FALLBACK_FALLBACK,
                _CONDUCTOR_EMERGENCY_FALLBACK,
                _CONDUCTOR_CANDIDATES_FALLBACK,
            )
        table = (
            tomllib.loads(path.read_text(encoding="utf-8")).get("routing", {}).get("conductor", {})
        )
        if not isinstance(table, dict):
            raise ValueError
        values = [
            table.get(k, fallback)
            for k, fallback in (
                ("default", _CONDUCTOR_DEFAULT_FALLBACK),
                ("fallback", _CONDUCTOR_FALLBACK_FALLBACK),
                ("emergency", _CONDUCTOR_EMERGENCY_FALLBACK),
            )
        ]
        candidates = table.get("candidates", list(_CONDUCTOR_CANDIDATES_FALLBACK))
        if (
            not all(isinstance(v, str) and v.strip() for v in values)
            or not isinstance(candidates, list)
            or not candidates
            or not all(isinstance(v, str) and v.strip() for v in candidates)
        ):
            raise ValueError
        return values[0], values[1], values[2], tuple(candidates)
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError):
        return (
            _CONDUCTOR_DEFAULT_FALLBACK,
            _CONDUCTOR_FALLBACK_FALLBACK,
            _CONDUCTOR_EMERGENCY_FALLBACK,
            _CONDUCTOR_CANDIDATES_FALLBACK,
        )


CONDUCTOR_DEFAULT, CONDUCTOR_FALLBACK, CONDUCTOR_EMERGENCY, CONDUCTOR_CANDIDATES = (
    _load_conductor_config()
)


def _model_ok(
    model: str,
    state: RuntimeState | None,
    meta: Any,
    env: Mapping[str, str],
) -> bool:
    if not credential_present(meta, env):
        return False
    if state is None:
        return True
    provider = getattr(meta, "provider", None)
    if not state.is_provider_available(provider):
        return False
    if not state.is_available(model) or state.circuit_open(model):
        return False
    return True


def resolve_conductor(
    state: RuntimeState | None = None,
    state_path: Any = None,
    env: Mapping[str, str] | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> ConductorHandoff:
    """Resolve the first available pre-session conductor candidate."""
    env = dict(os.environ if env is None else env)
    catalog = catalog if catalog is not None else load_provider_catalog()
    state = state if state is not None else load_state(state_path or default_state_path())

    skipped: list[str] = []
    for index, model in enumerate(CONDUCTOR_CANDIDATES):
        meta = catalog.get(model)
        if _model_ok(model, state, meta, env):
            return ConductorHandoff(
                model=model,
                provider=getattr(meta, "provider", "unknown"),
                provider_label=getattr(meta, "provider_label", "unknown"),
                reason="primary available"
                if index == 0
                else f"fallback after: {', '.join(skipped)}",
                degraded=index != 0,
                credential_present=True,
                available=True,
                circuit_open=False,
                source="state",
            )
        skipped.append(f"{model} unavailable/circuit-open/credential missing")

    # Every candidate is unhealthy. Use terminal Grok rather than returning a
    # known-bad primary candidate; this keeps failure behavior deterministic.
    model = CONDUCTOR_EMERGENCY
    meta = catalog.get(model)
    return ConductorHandoff(
        model=model,
        provider=getattr(meta, "provider", "unknown"),
        provider_label=getattr(meta, "provider_label", "unknown"),
        reason=f"all conductor candidates unhealthy: {', '.join(skipped)}",
        degraded=True,
        credential_present=credential_present(meta, env),
        available=False,
        circuit_open=state.circuit_open(model) if state else False,
        source="state",
    )


__all__ = [
    "CONDUCTOR_DEFAULT",
    "CONDUCTOR_FALLBACK",
    "CONDUCTOR_EMERGENCY",
    "CONDUCTOR_CANDIDATES",
    "resolve_conductor",
]
