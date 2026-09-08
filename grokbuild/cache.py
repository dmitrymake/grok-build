#!/usr/bin/env python3
"""Transactional decision cache for the Grok Build combine router.

The cache is keyed by (session_id, turn_id, prompt_key) with per-entry TTL and
bounded size, so concurrent hook processes and different turns do not clobber
each other. Writes use a single `flock` + unique tempfile + `os.replace`
(read-modify-write via `state.atomic_update_json`). The most recent decision is
also flattened onto the top level for backward compatibility with the legacy
`route.json` reader.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from grokbuild.redact import redact
from grokbuild.persist import atomic_update_json

CACHE_VERSION = 2
DEFAULT_TTL = 900.0  # seconds
MAX_ENTRIES = 50


def default_cache_path() -> Path:
    """Return the per-user route cache path."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "grok" / "route.json"


def cache_key(session_id: str | None, turn_id: int | None, prompt_key: str) -> str:
    """Build the cache key for a session, turn, and prompt."""
    return f"{session_id or ''}\x1f{turn_id or 0}\x1f{prompt_key}"


def load_cache(path: Path | str | None = None) -> dict[str, Any]:
    """Load the route cache or return an empty cache."""
    target = Path(path) if path is not None else default_cache_path()
    if not target.is_file():
        return {"version": CACHE_VERSION, "updated_at": 0.0, "entries": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": CACHE_VERSION, "updated_at": 0.0, "entries": {}}
    if not isinstance(data, Mapping):
        return {"version": CACHE_VERSION, "updated_at": 0.0, "entries": {}}
    return dict(data)


def _evict(cache: Mapping[str, Any], now: float) -> dict[str, Any]:
    entries = {
        k: dict(v) for k, v in (cache.get("entries") or {}).items() if isinstance(v, Mapping)
    }
    live: dict[str, Any] = {}
    for key, entry in entries.items():
        cached_at = float(entry.get("cached_at", 0.0))
        ttl = float(entry.get("ttl", DEFAULT_TTL))
        if now - cached_at <= ttl:
            live[key] = entry
    # Keep the newest MAX_ENTRIES by cached_at.
    ordered = sorted(live.items(), key=lambda kv: float(kv[1].get("cached_at", 0.0)), reverse=True)
    return dict(ordered[:MAX_ENTRIES])


def cache_put(
    record: Mapping[str, Any], path: Path | str | None = None, ttl: float | None = None
) -> None:
    """Redact and store a route record with bounded TTL and size."""
    ttl = DEFAULT_TTL if ttl is None else ttl
    target = Path(path) if path is not None else default_cache_path()

    def update(raw: Any) -> dict[str, Any]:
        now = time.time()
        cache = (
            dict(raw)
            if isinstance(raw, Mapping)
            else {
                "version": CACHE_VERSION,
                "updated_at": 0.0,
                "entries": {},
            }
        )
        cache["version"] = CACHE_VERSION
        cache["updated_at"] = now
        entries = _evict(cache, now)
        key = cache_key(
            record.get("session_id"),
            record.get("turn_id"),
            str(record.get("prompt_key") or ""),
        )
        entries[key] = {
            "record": redact(dict(record)),
            "cached_at": now,
            "ttl": ttl,
        }
        cache["entries"] = entries
        cache.update(redact(dict(record)))
        return cache

    atomic_update_json(target, update, default=None)


def cache_get(
    session_id: str | None,
    prompt_key: str | None = None,
    turn_id: int | None = None,
    path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a live cached route record for a session and prompt."""
    cache = load_cache(path)
    now = time.time()
    if prompt_key:
        key = cache_key(session_id, turn_id, prompt_key)
        entry = (cache.get("entries") or {}).get(key)
        if isinstance(entry, Mapping):
            cached_at = float(entry.get("cached_at", 0.0))
            ttl = float(entry.get("ttl", DEFAULT_TTL))
            if now - cached_at <= ttl:
                record = entry.get("record")
                return dict(record) if isinstance(record, Mapping) else None
        return None
    # Legacy top-level "last" lookup, session-scoped with TTL.
    record = cache
    if record.get("session_id") not in {None, session_id}:
        return None
    updated = float(record.get("updated_at", 0.0))
    if updated and now - updated > DEFAULT_TTL:
        return None
    return dict(record)


__all__ = [
    "default_cache_path",
    "cache_key",
    "cache_get",
    "cache_put",
    "CACHE_VERSION",
    "DEFAULT_TTL",
]
