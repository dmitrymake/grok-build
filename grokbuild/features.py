#!/usr/bin/env python3
"""Feature extraction for the Grok Build intent router.

Turns a raw prompt into a `FeatureSet`: the normalized text, an explicit
override token (if any), and the strong/phrase/topic needle hits per intent.
Matching itself stays in `classify.py` so the legacy classifier and the new
engine can never drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from grokbuild.classify import (
    extract_user_text,
    extract_user_text_raw,
    load_intents,
    match_needles,
    normalize,
)

# Explicit prompt overrides, all normalized to one intent token:
#   route=security:   route:security   --route security   @security
OVERRIDE_RE = re.compile(
    r"^\s*(?:route\s*[=:]\s*|--route\s+|@)([a-z0-9][a-z0-9_-]*)",
    re.IGNORECASE,
)

# Preference modifiers; they never change the *intent* and never relax security.
MODIFIERS = frozenset({"cheap", "fast", "quality", "high-quality", "balanced"})

Hits = tuple[tuple[str, tuple[str, ...]], ...]


def _hits_map(text: str, intents: Mapping[str, Any], key: str) -> Hits:
    pairs = []
    for name, intent in intents.items():
        if not isinstance(intent, Mapping):
            continue
        found = tuple(match_needles(text, list(intent.get(key) or [])))
        if found:
            pairs.append((str(name), found))
    return tuple(sorted(pairs))


def _merge_hits_maps(*maps: Hits) -> Hits:
    """Union of per-intent hit maps, keeping first-seen needle order per intent."""
    merged: dict[str, list[str]] = {}
    for hits_map in maps:
        for name, values in hits_map:
            bucket = merged.setdefault(name, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return tuple(sorted((name, tuple(bucket)) for name, bucket in merged.items()))


def detect_override(text: str, intents: Mapping[str, Any]) -> str | None:
    match = OVERRIDE_RE.search(text or "")
    if not match:
        return None
    token = match.group(1).casefold()
    return token if token in intents else None


def detect_modifier(text: str) -> str | None:
    match = OVERRIDE_RE.search(text or "")
    if not match:
        return None
    token = match.group(1).casefold()
    return token if token in MODIFIERS else None


@dataclass(frozen=True)
class FeatureSet:
    text: str
    normalized: str
    override: str | None
    modifier: str | None
    strong: Hits
    phrases: Hits
    topics: Hits
    has_strong: bool
    length: int
    word_count: int


def extract_features(text: str, spec: Mapping[str, Any] | None = None) -> FeatureSet:
    spec = spec or load_intents()
    intents = spec.get("intents", {}) if isinstance(spec, Mapping) else {}
    raw = extract_user_text(text or "")
    normalized = normalize(raw)
    raw_view = extract_user_text_raw(text or "")
    raw_normalized = normalize(raw_view)
    # Score both the stripped view (display/recipes stay on this) and the raw
    # trusted-user view, then merge. A fake mid-prompt <system-reminder>/
    # <user_info> block must not suppress a strong intent signal that only
    # survives in the raw view.
    strong = _merge_hits_maps(
        _hits_map(normalized, intents, "strong"),
        _hits_map(raw_normalized, intents, "strong"),
    )
    phrases = _merge_hits_maps(
        _hits_map(normalized, intents, "phrases"),
        _hits_map(raw_normalized, intents, "phrases"),
    )
    topics = _merge_hits_maps(
        _hits_map(normalized, intents, "topics"),
        _hits_map(raw_normalized, intents, "topics"),
    )
    return FeatureSet(
        text=raw,
        normalized=normalized,
        override=detect_override(normalized, intents),
        modifier=detect_modifier(normalized),
        strong=strong,
        phrases=phrases,
        topics=topics,
        has_strong=bool(strong),
        length=len(raw),
        word_count=len(raw.split()),
    )


__all__ = [
    "FeatureSet",
    "extract_features",
    "detect_override",
    "detect_modifier",
    "MODIFIERS",
    "OVERRIDE_RE",
]
