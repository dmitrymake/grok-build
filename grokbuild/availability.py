#!/usr/bin/env python3
"""Model endpoint and role availability checks."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from grokbuild import discovery
from grokbuild.evidence import FailureSignal
from grokbuild.roles import RoleRegistry, credential_present
from grokbuild.state import RuntimeState, default_state_path
from grokbuild.persist import atomic_update_json


def _cause_label(signal: FailureSignal) -> str:
    from grokbuild.evidence import classify_failure

    cause = classify_failure(signal)
    return {
        "auth": "auth-class",
        "environment": "infrastructure",
        "model": "model-class",
        "unknown": "unknown-class",
    }[cause]


def _tagged_failure(signal: FailureSignal) -> str:
    return f"{_cause_label(signal)}: {signal.detail() or 'unavailable'}"


def _endpoint_cache() -> Mapping[str, Any]:
    path = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "grok" / "models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = data.get("providers", {}) if isinstance(data, Mapping) else {}
    return providers if isinstance(providers, Mapping) else {}


QUOTA_SIGNAL_TTL = 30 * 60  # AWAITING OPERATIONAL TUNING.
REFRESH_MIN_INTERVAL = 5 * 60


def _quota_fetched_at(entry: Mapping[str, Any] | None) -> float | None:
    quota = entry.get("quota") if isinstance(entry, Mapping) else None
    fetched_at = quota.get("fetched_at") if isinstance(quota, Mapping) else None
    if not isinstance(fetched_at, (int, float)) and isinstance(entry, Mapping):
        fetched_at = entry.get("fetched_at")
    return float(fetched_at) if isinstance(fetched_at, (int, float)) else None


def _has_quota_windows(entry: Mapping[str, Any] | None) -> bool:
    quota = entry.get("quota") if isinstance(entry, Mapping) else None
    return isinstance(quota, Mapping) and isinstance(quota.get("windows"), list)


def _cache_entries(
    cache: Mapping[str, Any], key: str, provider: str
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Return the per-endpoint entry and the quota entry to judge it by.

    Quota is a property of the provider account, and the discovery probe
    writes it both under the provider and under every endpoint of that
    provider, at different moments. Judging an endpoint by whichever entry
    happens to exist under its own key lets a stale copy hide a fresh signal
    (and the reverse), so the freshest entry that carries quota windows wins;
    on a tie the endpoint's own entry is preferred.
    """
    specific = cache.get(key)
    specific = specific if isinstance(specific, Mapping) else None
    shared = cache.get(provider)
    shared = shared if isinstance(shared, Mapping) else None
    entry = specific if specific is not None else shared
    candidates = [item for item in (specific, shared) if _has_quota_windows(item)]
    if not candidates:
        return entry, entry
    quota_entry = max(
        candidates,
        key=lambda item: _quota_fetched_at(item) if _quota_fetched_at(item) is not None else -1.0,
    )
    return entry, quota_entry


def _quota_acceptable(entry: Mapping[str, Any] | None, *, now: float | None = None) -> bool | None:
    quota = entry.get("quota") if isinstance(entry, Mapping) else None
    windows = quota.get("windows") if isinstance(quota, Mapping) else None
    if not isinstance(windows, list):
        return True
    fetched_at = quota.get("fetched_at") if isinstance(quota, Mapping) else None
    if not isinstance(fetched_at, (int, float)) and isinstance(entry, Mapping):
        fetched_at = entry.get("fetched_at")
    if isinstance(fetched_at, (int, float)):
        age = (time.time() if now is None else now) - float(fetched_at)
        if age > QUOTA_SIGNAL_TTL:
            return None
    return not any(
        isinstance(window, Mapping)
        and isinstance(window.get("used_percent"), (int, float))
        and float(window["used_percent"]) >= 90.0
        for window in windows
    )


def _refresh_stale_quota(
    key: str, provider: str, cache: Mapping[str, Any], *, now: float | None = None
) -> bool:
    moment = time.time() if now is None else now
    targets, _ = discovery.load_targets()
    target = targets.get(key)
    if target is None or target.provider != provider:
        return False
    probe_name = target.quota_probe or provider
    if probe_name not in discovery.QUOTA_PROBES:
        return False
    attempted = [
        entry.get("attempted_at")
        for name, entry in cache.items()
        if isinstance(entry, Mapping)
        and name in targets
        and targets[name].provider == provider
        and isinstance(entry.get("attempted_at"), (int, float))
    ]
    if attempted and moment - max(float(value) for value in attempted) < REFRESH_MIN_INTERVAL:
        return False
    try:
        discovery.sync(targets={key: target}, now=moment, quota_only=True)
    except OSError:
        return False
    return True


def _model_endpoint_availability(
    state: RuntimeState | None,
    role_name: str,
    registry: RoleRegistry,
    session_id: str | None = None,
) -> tuple[bool, FailureSignal | None, dict[str, Any] | None]:
    role = registry.get(role_name)
    meta = registry.provider_catalog.get(role.model or "") if role else None
    if role is None or meta is None or not meta.explicit_endpoints:
        return True, None, None
    # Balance policy: `ordered` (or no session id) keeps the declared primary
    # first; `rotate` starts the same cyclic walk at a per-session stable index
    # so concurrent sessions spread across the endpoint pair.
    rotate = meta.balance == "rotate" and bool(session_id)
    start_index = (
        int(hashlib.sha256(session_id.encode()).hexdigest()[:16], 16) % len(meta.endpoints)
        if rotate
        else 0
    )
    visit_order = (
        [(start_index + offset) % len(meta.endpoints) for offset in range(len(meta.endpoints))]
        if rotate
        else list(range(len(meta.endpoints)))
    )
    cache = _endpoint_cache()
    skipped: list[dict[str, str]] = []
    first_signal: FailureSignal | None = None
    stale_candidate: tuple[Any, str, Any] | None = None
    for endpoint_index in visit_order:
        endpoint = meta.endpoints[endpoint_index]
        key = endpoint.availability_key or f"{role.model}@{endpoint.provider}"
        binding_id = endpoint.model_binding or (role.model if endpoint_index == 0 else None)
        binding = registry.executable_bindings.get(binding_id or "")
        entry, quota_entry = _cache_entries(cache, key, endpoint.provider)
        quota_acceptable = _quota_acceptable(quota_entry)
        if quota_acceptable is None and _refresh_stale_quota(key, endpoint.provider, cache):
            cache = _endpoint_cache()
            entry, quota_entry = _cache_entries(cache, key, endpoint.provider)
            quota_acceptable = _quota_acceptable(quota_entry)
        quota_fetched_at = _quota_fetched_at(quota_entry)
        if (
            quota_acceptable is True
            and quota_fetched_at is not None
            and time.time() - quota_fetched_at <= QUOTA_SIGNAL_TTL
            and state is not None
            and not state.is_provider_available(endpoint.provider)
        ):
            state_path = state.source_path or default_state_path()

            def clear_hold(raw: object) -> dict[str, object]:
                restored = (
                    RuntimeState.from_dict(raw) if isinstance(raw, Mapping) else RuntimeState()
                )
                restored.clear_provider_unavailable(endpoint.provider)
                return restored.to_dict()

            atomic_update_json(state_path, clear_hold, default=state.to_dict())
            state.clear_provider_unavailable(endpoint.provider)
        signal: FailureSignal | None = None
        if binding is None or not binding.base_url:
            signal = FailureSignal(
                reason="executable model binding missing", provider=endpoint.provider
            )
        elif endpoint.credential_env and binding.credential_env != endpoint.credential_env:
            signal = FailureSignal(
                reason="executable credential binding mismatch", provider=endpoint.provider
            )
        elif not credential_present(endpoint, os.environ):
            signal = FailureSignal(reason="credential missing", provider=endpoint.provider)
        elif state is not None and not state.is_provider_available(endpoint.provider):
            provider = state.provider_availability.get(endpoint.provider)
            signal = FailureSignal(
                reason=(provider.reason if provider else "") or "provider held",
                provider=endpoint.provider,
            )
        if signal is None and quota_acceptable is False:
            signal = FailureSignal(reason="quota pressured", provider=endpoint.provider)
        elif signal is None and quota_acceptable is None:
            stale_signal = FailureSignal(reason="quota-signal-stale", provider=endpoint.provider)
            stale_candidate = stale_candidate or (endpoint, key, binding)
            skipped.append(
                {
                    "endpoint": key,
                    "provider": endpoint.provider,
                    "reason": _tagged_failure(stale_signal),
                }
            )
            continue
        elif signal is None and endpoint.require_availability_signal:
            fetched_at = entry.get("fetched_at") if isinstance(entry, Mapping) else None
            if not isinstance(fetched_at, (int, float)):
                signal = FailureSignal(
                    reason="required availability signal missing", provider=endpoint.provider
                )
            elif time.time() - float(fetched_at) > 7 * 86400:
                signal = FailureSignal(
                    reason="required availability signal stale", provider=endpoint.provider
                )
            elif entry.get("status") != "ok":
                signal = FailureSignal(
                    reason="required availability signal unavailable",
                    provider=endpoint.provider,
                )
        if signal is None:
            return (
                True,
                None,
                {
                    "model": role.model,
                    "selected": key,
                    "provider": endpoint.provider,
                    "subscription_class": endpoint.subscription_class,
                    "executable_binding": {
                        "model": binding.model,
                        "base_url": binding.base_url,
                        "credential_env": binding.credential_env,
                    },
                    "role": role_name,
                    "skipped": skipped,
                    "balance_policy": meta.balance,
                    "rotate_start_index": start_index if rotate else None,
                },
            )
        first_signal = first_signal or signal
        skipped.append(
            {
                "endpoint": key,
                "provider": endpoint.provider,
                "reason": _tagged_failure(signal),
            }
        )
    if stale_candidate is not None:
        endpoint, key, binding = stale_candidate
        return (
            True,
            None,
            {
                "model": role.model,
                "selected": key,
                "provider": endpoint.provider,
                "subscription_class": endpoint.subscription_class,
                "executable_binding": {
                    "model": binding.model,
                    "base_url": binding.base_url,
                    "credential_env": binding.credential_env,
                },
                "role": role_name,
                "skipped": skipped,
                "balance_policy": meta.balance,
                "rotate_start_index": start_index if rotate else None,
            },
        )
    return (
        False,
        first_signal or FailureSignal(reason="all endpoints unavailable"),
        {
            "model": role.model,
            "selected": None,
            "provider": None,
            "subscription_class": None,
            "executable_binding": None,
            "role": role_name,
            "skipped": skipped,
            "balance_policy": meta.balance,
            "rotate_start_index": start_index if rotate else None,
        },
    )


def _role_availability(
    state: RuntimeState | None,
    role_name: str,
    registry: RoleRegistry,
    stale: bool,
    session_id: str | None = None,
    endpoint_events: list[dict[str, Any]] | None = None,
) -> tuple[bool, FailureSignal | None]:
    role = registry.get(role_name)
    if role is None:
        return False, FailureSignal(reason="role unavailable/unspawnable")
    endpoint_ok, endpoint_signal, endpoint_event = _model_endpoint_availability(
        state, role_name, registry, session_id
    )
    if endpoint_event is not None and endpoint_events is not None:
        endpoint_events.append(endpoint_event)
    if not endpoint_ok:
        return False, endpoint_signal
    meta = registry.provider_catalog.get(role.model or "")
    if meta is not None and meta.tier == "tryout":
        return False, FailureSignal(
            reason="tryout models are not role-routable", provider=role.provider
        )
    if (
        not (meta and meta.explicit_endpoints)
        and state is not None
        and not state.is_provider_available(role.provider)
    ):
        provider = state.provider_availability.get(role.provider)
        return False, FailureSignal(
            reason=(provider.reason if provider else "") or "provider unavailable",
            provider=role.provider,
        )
    if role.require_availability_signal and not (meta and meta.explicit_endpoints):
        if state is None:
            return False, FailureSignal(
                reason="required availability signal missing", provider=role.provider
            )
        if stale:
            return False, FailureSignal(
                reason="required availability signal stale", provider=role.provider
            )
        status = state.status_for(role_name)
        if not state.is_available(role_name, session_id=session_id) or state.circuit_open(
            role_name, session_id=session_id
        ):
            return False, FailureSignal(
                reason=status.reason or "role unavailable or circuit-open",
                provider=role.provider,
            )
        if not status.available or not status.availability_signal:
            return False, FailureSignal(
                reason=status.reason or "required availability signal missing",
                provider=role.provider,
            )
        return True, None
    if state is None or stale:
        return True, None
    if not state.is_available(role_name, session_id=session_id) or state.circuit_open(
        role_name, session_id=session_id
    ):
        status = state.status_for(role_name)
        return False, FailureSignal(
            reason=status.reason or "role unavailable or circuit-open",
            provider=role.provider,
        )
    return True, None


def _role_available(
    state: RuntimeState | None,
    role_name: str,
    registry: RoleRegistry,
    stale: bool,
    session_id: str | None = None,
) -> bool:
    return _role_availability(state, role_name, registry, stale, session_id)[0]


def _role_failed(state: RuntimeState | None, role_name: str, session_id: str | None = None) -> bool:
    if state is None:
        return False
    return (
        state.session_status_for(session_id, role_name).consecutive_failures > 0
        if session_id
        else (state.status_for(role_name).consecutive_failures > 0)
    )


def _role_pressured(state: RuntimeState | None, role_name: str) -> bool:
    if state is None:
        return False
    return state.status_for(role_name).quota_pressure() >= 0.9
