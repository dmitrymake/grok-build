#!/usr/bin/env python3
"""Upstream model discovery: advisory cached `GET /models` per provider.

Builds discovery targets from `config.toml` `[model.*]` blocks (grouped by
provider via `providers.json`), fetches each provider's OpenAI-shaped model
list with the configured bearer credential, and persists an advisory cache at
`~/.cache/grok/models.json` (mode 0600). The cache never contains credential
values and never mutates routing state, pins, or provider availability:
`diff` output is operator evidence for manual `config.toml` decisions only.

Routing normally uses local cached state. A stale quota signal can trigger one
bounded provider sync before endpoint selection; operators can also synchronize
explicitly via `python3 -m grokbuild.cli models sync`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import tomllib

from grokbuild.roles import load_provider_catalog, provider_from, resolve_config_path
from grokbuild.persist import atomic_update_json

CACHE_VERSION = 1
FETCH_TIMEOUT = 4.0
STALE_AFTER = 7 * 86400
MAX_BODY_BYTES = 2_000_000

FetchFn = Callable[[str, str], tuple[int | None, list[str] | None, str | None]]


def default_models_cache_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "grok" / "models.json"


@dataclass(frozen=True)
class DiscoveryTarget:
    provider: str
    label: str
    base_url: str
    env_key: str | None
    catalog_ids: tuple[str, ...]
    raw_ids: tuple[str, ...]
    quota_probe: str | None = None


def load_targets(
    config_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> tuple[dict[str, DiscoveryTarget], list[str]]:
    """Return provider -> target plus non-fatal warnings; never raises."""
    warnings: list[str] = []
    catalog = load_provider_catalog(providers_path)
    quota_fields: dict[str, str] = {}
    try:
        provider_file = (
            Path(providers_path)
            if providers_path is not None
            else Path(__file__).resolve().parent / "providers.json"
        )
        raw_providers = json.loads(provider_file.read_text(encoding="utf-8")).get("providers", {})
        if isinstance(raw_providers, Mapping):
            quota_fields = {
                str(name): spec["quota_probe"]
                for name, spec in raw_providers.items()
                if isinstance(spec, Mapping) and isinstance(spec.get("quota_probe"), str)
            }
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    path = resolve_config_path(config_path)
    if path is None:
        return {}, ["config.toml not found; no discovery targets"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"config.toml unreadable ({exc}); no discovery targets"]

    model_blocks = data.get("model", {}) if isinstance(data, Mapping) else {}
    if not isinstance(model_blocks, Mapping):
        return {}, warnings

    provider_bindings: dict[str, tuple[str, str | None]] = {}
    for key, spec in model_blocks.items():
        if not isinstance(spec, Mapping):
            continue
        base_url = str(spec.get("base_url") or "").strip()
        if not base_url:
            continue
        meta = catalog.get(str(key))
        raw_key = str(key)
        provider = (
            meta.provider
            if meta
            else (raw_key.rsplit("@", 1)[1] if "@" in raw_key else provider_from(base_url))
        )
        provider_bindings.setdefault(
            provider,
            (base_url, str(spec.get("env_key")) if spec.get("env_key") else None),
        )

    grouped: dict[str, dict[str, Any]] = {}
    for key, spec in model_blocks.items():
        if not isinstance(spec, Mapping):
            continue
        base_url = str(spec.get("base_url") or "").strip()
        if not base_url:
            continue
        meta = catalog.get(str(key))
        raw_id = str(spec.get("model") or key)
        endpoints = meta.endpoints if meta and meta.explicit_endpoints else ()
        candidates = endpoints or (None,)
        for endpoint in candidates:
            provider = (
                endpoint.provider
                if endpoint
                else (
                    meta.provider
                    if meta
                    else (
                        str(key).rsplit("@", 1)[1] if "@" in str(key) else provider_from(base_url)
                    )
                )
            )
            target_name = f"{key}@{provider}" if endpoint else provider
            binding = provider_bindings.get(provider)
            endpoint_url = (
                binding[0]
                if binding
                else (base_url if provider == (meta.provider if meta else provider) else "")
            )
            if not endpoint_url:
                warnings.append(
                    f"endpoint '{target_name}': no configured provider base_url; skipping discovery"
                )
                continue
            env_key = (
                endpoint.credential_env
                if endpoint and endpoint.credential_env
                else (
                    binding[1]
                    if binding
                    else (str(spec.get("env_key")) if spec.get("env_key") else None)
                )
            )
            label = (
                endpoint.provider_label if endpoint else (meta.provider_label if meta else provider)
            )
            group = grouped.setdefault(
                target_name,
                {
                    "provider": provider,
                    "label": label,
                    "base_url": endpoint_url,
                    "env_key": env_key,
                    "catalog_ids": [],
                    "raw_ids": [],
                    "quota_probe": quota_fields.get(provider),
                },
            )
            if group["base_url"] != endpoint_url:
                warnings.append(f"endpoint '{target_name}': mixed base_url; keeping first")
            elif group["env_key"] != env_key:
                warnings.append(f"endpoint '{target_name}': mixed env_key; keeping first")
            group["catalog_ids"].append(str(key))
            group["raw_ids"].append(raw_id)

    targets = {
        name: DiscoveryTarget(
            provider=str(group["provider"]),
            label=str(group["label"]),
            base_url=str(group["base_url"]),
            env_key=group["env_key"],
            catalog_ids=tuple(sorted(group["catalog_ids"])),
            raw_ids=tuple(sorted(group["raw_ids"])),
            quota_probe=group.get("quota_probe"),
        )
        for name, group in grouped.items()
    }
    return targets, warnings


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_AUTHENTICATED_OPENER = urllib.request.build_opener(_NoRedirect())


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.casefold()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else None)
    return scheme, (parsed.hostname or "").casefold(), port


def _get_json(url: str, bearer: str) -> tuple[int | None, Any, str | None]:
    """Authorized GET returning parsed JSON without forwarding credentials."""
    current = url
    try:
        current_origin = _origin(current)
    except ValueError:
        return None, None, "invalid-auth-url"
    if current_origin[0] != "https":
        return None, None, "insecure-auth-url"
    for _redirect in range(4):
        request = urllib.request.Request(
            current,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json",
                "User-Agent": "grok-build-discovery/1",
            },
        )
        try:
            with _AUTHENTICATED_OPENER.open(request, timeout=FETCH_TIMEOUT) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(MAX_BODY_BYTES)
                break
        except urllib.error.HTTPError as exc:
            if int(exc.code) not in {301, 302, 303, 307, 308}:
                return int(exc.code), None, f"http-{exc.code}"
            location = exc.headers.get("Location") if exc.headers is not None else None
            if not location:
                return int(exc.code), None, "redirect-missing-location"
            try:
                redirected = urllib.parse.urljoin(current, location)
                redirected_origin = _origin(redirected)
            except ValueError:
                return int(exc.code), None, "unsafe-auth-redirect"
            if redirected_origin[0] != "https" or redirected_origin != current_origin:
                return int(exc.code), None, "unsafe-auth-redirect"
            current = redirected
            current_origin = redirected_origin
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, None, f"network:{type(exc).__name__}"
    else:
        return None, None, "too-many-redirects"
    try:
        return status, json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError):
        return status, None, "invalid-json"


def _default_fetch(base_url: str, bearer: str) -> tuple[int | None, list[str] | None, str | None]:
    status, payload, error = _get_json(base_url.rstrip("/") + "/models", bearer)
    if error is not None:
        return status, None, error
    items = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        return status, None, "unexpected-shape"
    ids: set[str] = set()
    for item in items:
        model_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(model_id, str) and model_id:
            ids.add(model_id)
    return status, sorted(ids), None


def _iso(value: Any) -> str | None:
    """Normalize an epoch number or ISO-ish string to an ISO-8601 UTC string."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit():
            value = float(text)
        else:
            return text
    if isinstance(value, (int, float)) and value > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    return None


def _quota_opencode(base_url: str, bearer: str) -> dict[str, Any] | None:
    """OpenCode Go GET /usage: rolling/weekly/monthly percent + resetsAt."""
    _, payload, error = _get_json(base_url.rstrip("/") + "/usage", bearer)
    if error is not None or not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    windows: list[dict[str, Any]] = []
    for name in ("rolling", "weekly", "monthly"):
        window = usage.get(name)
        if isinstance(window, Mapping) and isinstance(window.get("percent"), (int, float)):
            windows.append(
                {
                    "name": name,
                    "used_percent": float(window["percent"]),
                    "resets_at": _iso(window.get("resetsAt")),
                }
            )
    return {"plan": None, "windows": windows} if windows else None


def _quota_codex(base_url: str, bearer: str) -> dict[str, Any] | None:
    """Configured model proxy GET /v1/usage: last-seen provider headers.

    The snapshot is populated passively by real role traffic; an empty snapshot
    (proxy restarted, no calls yet) is not an error, just no quota data.
    """
    _, payload, error = _get_json(base_url.rstrip("/") + "/usage", bearer)
    if error is not None or not isinstance(payload, Mapping):
        return None
    headers = payload.get("headers")
    if not isinstance(headers, Mapping) or not headers:
        return None

    def _percent(key: str) -> float | None:
        value = str(headers.get(key) or "").strip()
        try:
            return float(value)
        except ValueError:
            return None

    windows: list[dict[str, Any]] = []
    for prefix, name in (("x-codex-primary", "primary"), ("x-codex-secondary", "secondary")):
        used = _percent(f"{prefix}-used-percent")
        if used is None:
            continue
        window: dict[str, Any] = {"name": name, "used_percent": used}
        minutes = _percent(f"{prefix}-window-minutes")
        if minutes:
            window["window_minutes"] = int(minutes)
        reset = _iso(headers.get(f"{prefix}-reset-at"))
        if reset:
            window["resets_at"] = reset
        windows.append(window)
    if not windows:
        return None
    quota: dict[str, Any] = {
        "plan": str(headers.get("x-codex-plan-type") or "") or None,
        "windows": windows,
    }
    if isinstance(payload.get("captured_at"), (int, float)):
        quota["captured_at"] = float(payload["captured_at"])
    return quota


# Providers with a verified remaining-quota probe. Other configured providers
# expose no known usage endpoint on their inference hosts;
# extend this table only with live-verified probes.
QUOTA_PROBES: dict[str, Callable[[str, str], dict[str, Any] | None]] = {
    "opencode": _quota_opencode,
    "codex": _quota_codex,
}


def _valid_root(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "providers": {}}
    providers = raw.get("providers")
    if not isinstance(providers, Mapping):
        return {"version": CACHE_VERSION, "providers": {}}
    return {
        "version": CACHE_VERSION,
        "providers": {str(k): dict(v) for k, v in providers.items() if isinstance(v, Mapping)},
    }


def load_cache(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else default_models_cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _valid_root(None)
    return _valid_root(raw)


def sync(
    targets: Mapping[str, DiscoveryTarget] | None = None,
    env: Mapping[str, str] | None = None,
    fetch: FetchFn | None = None,
    path: Path | str | None = None,
    now: float | None = None,
    quota_probes: Mapping[str, Callable[[str, str], dict[str, Any] | None]] | None = None,
    quota_only: bool = False,
) -> dict[str, Any]:
    """Fetch every credentialed provider and update the advisory cache.

    A failed attempt keeps the previous successful model list (with its old
    `fetched_at`) and records the sanitized error; a provider whose credential
    is unset is skipped without a network attempt. Providers listed in
    `QUOTA_PROBES` also record a normalized remaining-quota snapshot; a quota
    failure never fails the model sync and keeps the previous snapshot.
    """
    if targets is None:
        targets, _ = load_targets()
    env = os.environ if env is None else env
    fetch = _default_fetch if fetch is None else fetch
    probes = QUOTA_PROBES if quota_probes is None else quota_probes
    cache_path = Path(path) if path is not None else default_models_cache_path()
    stamp = time.time() if now is None else now
    cached_before = load_cache(cache_path) if quota_only else {"providers": {}}

    results: dict[str, dict[str, Any]] = {}
    for name, target in sorted(targets.items()):
        bearer = (env.get(target.env_key) or "").strip() if target.env_key else ""
        previous = cached_before["providers"].get(name)
        entry: dict[str, Any] = dict(previous) if isinstance(previous, Mapping) else {}
        entry.update(
            {
                "label": target.label,
                "base_url": target.base_url,
                "attempted_at": stamp,
            }
        )
        if not bearer:
            entry.update(
                {"status": "credential-unset", "http_status": None, "error": "credential-unset"}
            )
        else:
            if not quota_only:
                http_status, models, error = fetch(target.base_url, bearer)
                entry["http_status"] = http_status
                if models is not None:
                    normalized = sorted({str(m) for m in models if isinstance(m, str) and m})
                    entry.update(
                        {
                            "status": "ok",
                            "error": None,
                            "models": normalized,
                            "fetched_at": stamp,
                        }
                    )
                else:
                    entry.update({"status": "error", "error": error})
            probe = probes.get(name) or (
                probes.get(target.quota_probe) if target.quota_probe else None
            )
            if probe is not None:
                quota = probe(target.base_url, bearer)
                if quota is not None:
                    entry["quota"] = {**quota, "fetched_at": stamp}
        results[name] = entry

    def update(raw: Any) -> dict[str, Any]:
        root = _valid_root(raw)
        providers = root["providers"]
        for name, entry in results.items():
            previous = providers.get(name)
            if isinstance(previous, Mapping):
                # preserve last-good data and its fetch stamp across failures
                if "models" not in entry:
                    if isinstance(previous.get("models"), list):
                        entry["models"] = list(previous["models"])
                    if isinstance(previous.get("fetched_at"), (int, float)):
                        entry["fetched_at"] = float(previous["fetched_at"])
                if "quota" not in entry and isinstance(previous.get("quota"), Mapping):
                    entry["quota"] = dict(previous["quota"])
            providers[name] = entry
        return root

    atomic_update_json(cache_path, update, default=None)
    return {"version": CACHE_VERSION, "providers": results}


def stale_targets(
    targets: Mapping[str, DiscoveryTarget],
    cache: Mapping[str, Any],
    max_age_seconds: float,
    *,
    now: float | None = None,
) -> dict[str, DiscoveryTarget]:
    """Return targets whose newest quota/model signal exceeds the age limit."""
    moment = time.time() if now is None else now
    providers = cache.get("providers", {}) if isinstance(cache, Mapping) else {}
    selected: dict[str, DiscoveryTarget] = {}
    for name, target in targets.items():
        entry = providers.get(name) if isinstance(providers, Mapping) else None
        quota = entry.get("quota") if isinstance(entry, Mapping) else None
        fetched_at = quota.get("fetched_at") if isinstance(quota, Mapping) else None
        if not isinstance(fetched_at, (int, float)) and isinstance(entry, Mapping):
            fetched_at = entry.get("fetched_at")
        if not isinstance(fetched_at, (int, float)) or moment - float(fetched_at) > max_age_seconds:
            selected[name] = target
    return selected


def diff(
    targets: Mapping[str, DiscoveryTarget] | None = None,
    cache: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Compare configured raw model ids with the cached upstream lists.

    `missing_upstream` are configured pins absent from the provider's list
    (dead-pin warnings); `unbound` are upstream models with no
    `config.toml` binding (manual candidates). Advisory only.
    """
    if targets is None:
        targets, _ = load_targets()
    cache = load_cache() if cache is None else _valid_root(cache)
    moment = time.time() if now is None else now

    out: dict[str, Any] = {"providers": {}}
    cached_providers = cache.get("providers", {})
    for name, target in sorted(targets.items()):
        entry = cached_providers.get(name)
        report: dict[str, Any] = {
            "label": target.label,
            "configured": list(target.raw_ids),
        }
        models = entry.get("models") if isinstance(entry, Mapping) else None
        fetched_at = entry.get("fetched_at") if isinstance(entry, Mapping) else None
        if isinstance(entry, Mapping):
            report["status"] = str(entry.get("status") or "unknown")
        else:
            report["status"] = "unsynced"
        if isinstance(fetched_at, (int, float)):
            age = max(0.0, moment - float(fetched_at))
            report["age_seconds"] = age
            report["stale"] = age > STALE_AFTER
        else:
            report["age_seconds"] = None
            report["stale"] = None
        if isinstance(models, list):
            upstream = {str(m) for m in models}
            configured = set(target.raw_ids)
            report["missing_upstream"] = sorted(configured - upstream)
            report["unbound"] = sorted(upstream - configured)
        else:
            report["missing_upstream"] = None
            report["unbound"] = None
        out["providers"][name] = report
    return out


def quota_report(
    targets: Mapping[str, DiscoveryTarget] | None = None,
    cache: Mapping[str, Any] | None = None,
    now: float | None = None,
    quota_probes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-provider remaining-quota view from the advisory cache."""
    if targets is None:
        targets, _ = load_targets()
    cache = load_cache() if cache is None else _valid_root(cache)
    probes = QUOTA_PROBES if quota_probes is None else quota_probes
    moment = time.time() if now is None else now

    out: dict[str, Any] = {"providers": {}}
    cached_providers = cache.get("providers", {})
    for name, target in sorted(targets.items()):
        entry = cached_providers.get(name)
        quota = entry.get("quota") if isinstance(entry, Mapping) else None
        report: dict[str, Any] = {
            "label": target.label,
            "supported": bool(
                probes.get(name) or (target.quota_probe and probes.get(target.quota_probe))
            ),
            "quota": dict(quota) if isinstance(quota, Mapping) else None,
        }
        fetched_at = quota.get("fetched_at") if isinstance(quota, Mapping) else None
        report["age_seconds"] = (
            max(0.0, moment - float(fetched_at)) if isinstance(fetched_at, (int, float)) else None
        )
        out["providers"][name] = report
    return out


__all__ = [
    "CACHE_VERSION",
    "QUOTA_PROBES",
    "STALE_AFTER",
    "DiscoveryTarget",
    "default_models_cache_path",
    "diff",
    "load_cache",
    "load_targets",
    "quota_report",
    "stale_targets",
    "sync",
]
