#!/usr/bin/env python3
"""Private bounded cache for validated visual-intake results."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Mapping

from grokbuild.persist import atomic_update_json

CACHE_VERSION = 2
TTL = 86400
MAX_ENTRIES = 100


def default_visual_cache_path() -> Path:
    """Return the per-user visual-intake cache path."""
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "grok" / "visual-intake.json"


def _valid_root(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "entries": {}}
    entries = raw.get("entries")
    if not isinstance(entries, Mapping):
        return {"version": CACHE_VERSION, "entries": {}}
    return {"version": CACHE_VERSION, "entries": dict(entries)}


def visual_cache_get(
    key: str, path: Path | str | None = None, now: float | None = None
) -> dict[str, Any] | None:
    """Return a live cached visual result by key."""
    target = Path(path) if path is not None else default_visual_cache_path()
    now = time.time() if now is None else now
    try:
        import json

        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping) or raw.get("version") != CACHE_VERSION:
        return None
    root = _valid_root(raw)
    item = root["entries"].get(key)
    if not isinstance(item, Mapping) or not isinstance(item.get("created_at"), (int, float)):
        return None
    if now - float(item["created_at"]) > TTL:
        return None
    value = item.get("value")
    return dict(value) if isinstance(value, Mapping) else None


def visual_cache_put(
    key: str, value: Mapping[str, Any], path: Path | str | None = None, now: float | None = None
) -> None:
    """Store a bounded, expiring visual result cache entry."""
    target = Path(path) if path is not None else default_visual_cache_path()
    stamp = time.time() if now is None else now

    def update(raw: Any) -> dict[str, Any]:
        root = _valid_root(raw)
        entries = {
            str(k): dict(v)
            for k, v in root["entries"].items()
            if isinstance(v, Mapping)
            and isinstance(v.get("created_at"), (int, float))
            and stamp - float(v["created_at"]) <= TTL
        }
        entries[key] = {"created_at": stamp, "value": dict(value)}
        if len(entries) > MAX_ENTRIES:
            oldest = sorted(entries, key=lambda k: float(entries[k]["created_at"]))
            for stale in oldest[: len(entries) - MAX_ENTRIES]:
                entries.pop(stale, None)
        return {"version": CACHE_VERSION, "entries": entries}

    atomic_update_json(target, update, default={"version": CACHE_VERSION, "entries": {}})
    try:
        target.chmod(0o600)
    except OSError:
        pass


def visual_cache_stats(path: Path | str | None = None, now: float | None = None) -> dict[str, Any]:
    """Return live visual cache entry count and path."""
    target = Path(path) if path is not None else default_visual_cache_path()
    now = time.time() if now is None else now
    try:
        import json

        root = _valid_root(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"version": CACHE_VERSION, "entries": 0, "path": str(target)}
    count = sum(
        isinstance(v, Mapping)
        and isinstance(v.get("created_at"), (int, float))
        and now - float(v["created_at"]) <= TTL
        for v in root["entries"].values()
    )
    return {"version": CACHE_VERSION, "entries": count, "path": str(target)}


def visual_cache_clear(path: Path | str | None = None) -> None:
    """Clear the visual-intake cache."""
    target = Path(path) if path is not None else default_visual_cache_path()
    atomic_update_json(target, lambda _raw: {"version": CACHE_VERSION, "entries": {}}, default=None)


__all__ = [
    "CACHE_VERSION",
    "TTL",
    "MAX_ENTRIES",
    "default_visual_cache_path",
    "visual_cache_get",
    "visual_cache_put",
    "visual_cache_stats",
    "visual_cache_clear",
]
