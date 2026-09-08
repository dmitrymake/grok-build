#!/usr/bin/env python3
"""Read-only metrics over schema-v4, generation-1 Grok route telemetry.

Intent routing is routed submits / all submits. Pair divergence compares each
routed submit's effective ``(model, effort)`` pair with the configured default
pair; a null routed effort inherits ``default_reasoning_effort``. The selected
pair distribution preserves raw effort, rendering null as ``inherit``. Legacy
``model_divergence`` remains for one telemetry generation and is deprecated.
Tier fallback divergence is divergent routed submits / routed submits.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from ._repo import config_path as repo_config_path
from typing import Any, Mapping

from grokbuild.decision import SCHEMA_VERSION, TELEMETRY_GENERATION
from grokbuild.persist import parse_iso_utc
from grokbuild.roles import load_registry
from grokbuild.state import default_log_path, default_state_path


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        raise ValueError("--since must be an ISO-8601 timestamp") from exc


def _observed(record: Mapping[str, Any]) -> datetime | None:
    value = record.get("observed_at") or record.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso_utc(value)
    except ValueError:
        return None


def log_paths(path: Path | str | None = None) -> tuple[Path, ...]:
    """Return the current and two rotated telemetry log paths."""
    current = Path(path) if path is not None else default_log_path()
    return (Path(f"{current}.2"), Path(f"{current}.1"), current)


def read_records(path: Path | str | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read valid telemetry records and report ingestion counts."""
    records: list[dict[str, Any]] = []
    counts = {"lines": 0, "skipped": 0, "malformed": 0}
    for candidate in log_paths(path):
        if not candidate.is_file():
            continue
        try:
            handle = candidate.open("r", encoding="utf-8")
        except OSError:
            counts["malformed"] += 1
            continue
        with handle:
            for line in handle:
                counts["lines"] += 1
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    counts["malformed"] += 1
                    continue
                if not isinstance(item, dict):
                    counts["malformed"] += 1
                    continue
                if item.get("event") == "visual_intake":
                    records.append(item)
                    continue
                if (
                    item.get("version") != SCHEMA_VERSION
                    or item.get("telemetry_generation") != TELEMETRY_GENERATION
                ):
                    counts["skipped"] += 1
                    continue
                if not isinstance(item.get("decision_id"), str) or not item.get("decision_id"):
                    counts["malformed"] += 1
                    continue
                records.append(item)
    counts["accepted"] = len(records)
    return records, counts


def _load_labels(path: Path | str | None) -> dict[str, str]:
    target = Path(path) if path is not None else default_state_path().parent / "labels.json"
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    allowed = {"correct", "false_positive", "false_negative", "uncertain"}
    return (
        {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v in allowed}
        if isinstance(raw, dict)
        else {}
    )


def compute_stats(
    *,
    log: Path | str | None = None,
    labels: Path | str | None = None,
    since: str | None = None,
    session: str | None = None,
    repo: str | None = None,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Compute filtered routing, gate, fallback, label, and visual telemetry statistics."""
    records, ingestion = read_records(log)
    cutoff = _parse_since(since)
    filtered: list[dict[str, Any]] = []
    for item in records:
        if session and item.get("session_id") != session:
            continue
        if repo and item.get("repo_id") != repo:
            continue
        if cutoff is not None:
            stamp = _observed(item)
            if stamp is None or stamp < cutoff:
                continue
        filtered.append(item)

    submits_by_id: dict[str, dict[str, Any]] = {}
    for item in filtered:
        if item.get("event") == "user_prompt_submit":
            submits_by_id.setdefault(str(item["decision_id"]), item)
    submits = list(submits_by_id.values())
    routed = [item for item in submits if item.get("role") and item.get("intent")]

    registry = load_registry(config_path)
    default_model = None
    default_effort = None
    config_available = False
    try:
        import tomllib

        config = Path(config_path) if config_path else repo_config_path()
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
        models = raw.get("models", {})
        if (
            isinstance(models, Mapping)
            and models.get("default") is not None
            and models.get("default_reasoning_effort") is not None
        ):
            default_model = str(models["default"])
            default_effort = str(models["default_reasoning_effort"])
            config_available = True
    except (OSError, ValueError):
        pass
    try:
        from grokbuild.classify import load_intents

        intents = load_intents().get("intents", {})
    except (OSError, ValueError):
        intents = {}

    model_divergent = (
        sum(item.get("model") != default_model for item in routed) if config_available else None
    )
    pair_divergent = None
    if config_available:
        pair_divergent = sum(
            (item.get("model"), item.get("reasoning_effort") or default_effort)
            != (default_model, default_effort)
            for item in routed
        )
    pair_counts = Counter(
        (
            str(item.get("model") or "unknown"),
            "inherit"
            if item.get("reasoning_effort") is None
            else str(item.get("reasoning_effort")),
        )
        for item in routed
    )
    selected_pair_distribution = [
        {"model": model, "effort": effort, "count": count, "rate": _rate(count, len(routed))}
        for (model, effort), count in sorted(pair_counts.items())
    ]
    tier_divergent = 0
    for item in routed:
        intent_spec = intents.get(item.get("intent"), {}) if isinstance(intents, Mapping) else {}
        expected = registry.canonical(str(intent_spec.get("role") or item.get("intent")))
        selected = registry.canonical(str(item.get("role")))
        evidence = (
            bool(item.get("fallbacks"))
            or item.get("role_available") is False
            or any(
                "degraded" in str(w).casefold() or "fallback" in str(w).casefold()
                for w in (item.get("warnings") or [])
            )
        )
        if selected != expected or evidence:
            tier_divergent += 1

    pre = [
        item
        for item in filtered
        if item.get("event") in {"pre_tool_use_outcome", "pre_tool_use"} and "outcome" in item
    ]
    pre = [item for item in pre if not item.get("subagent_exemption")]
    denies = [item for item in pre if item.get("outcome") == "deny"]
    deny_reasons = Counter(str(item.get("reason_code") or "unknown") for item in denies)
    gate_denies = [
        item
        for item in denies
        if item.get("reason_code") not in {"zero_write", "unknown_write_tool"}
    ]
    zero_write = [item for item in denies if item.get("reason_code") == "zero_write"]

    stops = [item for item in filtered if item.get("event") == "stop"]
    eligible_stops = [
        item
        for item in stops
        if item.get("reason") == "end_turn" and not item.get("stop_hook_active")
    ]
    blocked_stops = [item for item in eligible_stops if item.get("blocked")]

    friction = Counter(
        (
            str(item["decision_id"]),
            str(item.get("reason_code") or "unknown"),
            str(item.get("next_stage") or ""),
        )
        for item in denies
    )
    repeated = {"|".join(key): count for key, count in sorted(friction.items()) if count > 1}

    label_map = _load_labels(labels)
    submit_ids = set(submits_by_id)
    labeled = {key: value for key, value in label_map.items() if key in submit_ids}
    evaluable = [value for value in labeled.values() if value in {"correct", "false_positive"}]
    false_positives = sum(value == "false_positive" for value in evaluable)

    visual = [item for item in filtered if item.get("event") == "visual_intake"]
    visual_cached = sum(item.get("visual_intake_cached") is True for item in visual)
    visual_fallback = sum(item.get("visual_intake_fallback_used") is True for item in visual)

    return {
        "schema_version": SCHEMA_VERSION,
        "telemetry_generation": TELEMETRY_GENERATION,
        "ingestion": {**ingestion, "filtered": len(filtered)},
        "submits": {
            "all": len(submits),
            "routed": len(routed),
            "deduplicated": sum(1 for i in filtered if i.get("event") == "user_prompt_submit")
            - len(submits),
        },
        "intent_routing": {
            "numerator": len(routed),
            "denominator": len(submits),
            "rate": _rate(len(routed), len(submits)),
        },
        "model_divergence": {
            "numerator": model_divergent,
            "denominator": len(routed),
            "rate": _rate(model_divergent, len(routed)) if model_divergent is not None else None,
            "configured_default": default_model,
        },
        "pair_divergence": (
            {
                "numerator": pair_divergent,
                "denominator": len(routed),
                "rate": _rate(pair_divergent, len(routed)),
                "configured_default": {"model": default_model, "effort": default_effort},
            }
            if pair_divergent is not None
            else None
        ),
        "selected_pair_distribution": selected_pair_distribution,
        "tier_fallback_divergence": {
            "numerator": tier_divergent,
            "denominator": len(routed),
            "rate": _rate(tier_divergent, len(routed)),
        },
        "pre_tool_denies": {
            "numerator": len(denies),
            "denominator": len(pre),
            "rate": _rate(len(denies), len(pre)),
            "by_reason_code": dict(sorted(deny_reasons.items())),
        },
        "gate_denies": {
            "numerator": len(gate_denies),
            "denominator": len(pre),
            "rate": _rate(len(gate_denies), len(pre)),
        },
        "zero_write": {
            "numerator": len(zero_write),
            "denominator": len(pre),
            "rate": _rate(len(zero_write), len(pre)),
        },
        "stop_blocks": {
            "numerator": len(blocked_stops),
            "denominator": len(eligible_stops),
            "rate": _rate(len(blocked_stops), len(eligible_stops)),
        },
        "repeat_friction": {
            "groups": repeated,
            "repeated_denies": sum(count - 1 for count in friction.values() if count > 1),
        },
        "labels": {
            "available": len(label_map),
            "covered": len(labeled),
            "coverage": _rate(len(labeled), len(submits)),
            "false_positives": false_positives,
            "fpr": _rate(false_positives, len(evaluable)),
        },
        "visual_intake": {
            "events": len(visual),
            "cached": visual_cached,
            "fallback_used": visual_fallback,
        },
    }


def human_summary(stats: Mapping[str, Any]) -> str:
    """Render computed statistics as a human-readable summary."""

    def metric(name: str) -> str:
        item = stats[name]
        if item is None:
            return "unavailable"
        rate = item["rate"]
        rendered = "n/a" if rate is None else f"{rate:.1%}"
        return f"{rendered} ({item['numerator']}/{item['denominator']})"

    ingestion = stats["ingestion"]
    return "\n".join(
        (
            f"accepted={ingestion['accepted']} skipped={ingestion['skipped']} malformed={ingestion['malformed']}",
            f"intent routing: {metric('intent_routing')}",
            f"pair divergence: {metric('pair_divergence')}",
            f"model divergence (deprecated): {metric('model_divergence')}",
            f"tier/fallback divergence: {metric('tier_fallback_divergence')}",
            f"PreToolUse deny: {metric('pre_tool_denies')} (gate {metric('gate_denies')}; zero-write {metric('zero_write')})",
            f"Stop block: {metric('stop_blocks')}",
        )
    )


__all__ = ["compute_stats", "human_summary", "log_paths", "read_records"]
