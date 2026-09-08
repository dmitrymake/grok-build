"""Versioned out-of-band endpoint-resolution records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from grokbuild.persist import _read_decision_history_unbounded, append_jsonl, sidecar_path

ENDPOINT_RESOLUTION_SCHEMA = "endpoint-resolution-v1"
_MAX_SKIPS = 16
_MAX_VALUE = 500


@dataclass(frozen=True)
class EndpointResolutionRecord:
    decision_id: str
    session_id: str | None
    model: str
    selected: str | None
    provider: str | None
    subscription_class: str | None
    executable_binding: dict[str, str | None] | None
    skipped: tuple[dict[str, str], ...]
    observed_at: str
    schema: str = ENDPOINT_RESOLUTION_SCHEMA
    balance_policy: str | None = None
    rotate_start_index: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["skipped"] = list(self.skipped)
        return payload


def endpoint_resolution_path() -> Path:
    return sidecar_path("endpoint-resolution-v1.jsonl")


def make_endpoint_resolution(
    *,
    decision_id: str,
    session_id: str | None,
    event: Mapping[str, Any],
    observed_at: str | None = None,
) -> EndpointResolutionRecord:
    raw_skips = event.get("skipped")
    bounded_skips = raw_skips[:_MAX_SKIPS] if isinstance(raw_skips, (list, tuple)) else ()
    skipped = tuple(
        {
            "endpoint": str(item.get("endpoint") or "")[:_MAX_VALUE],
            "provider": str(item.get("provider") or "")[:_MAX_VALUE],
            "reason": str(item.get("reason") or "endpoint unavailable")[:_MAX_VALUE],
        }
        for item in bounded_skips
        if isinstance(item, Mapping)
    )
    raw_binding = event.get("executable_binding")
    executable_binding = (
        {
            "model": str(raw_binding.get("model") or "")[:_MAX_VALUE],
            "base_url": str(raw_binding.get("base_url") or "")[:_MAX_VALUE],
            "credential_env": str(raw_binding.get("credential_env") or "")[:_MAX_VALUE],
        }
        if isinstance(raw_binding, Mapping)
        else None
    )
    raw_balance = event.get("balance_policy")
    raw_start = event.get("rotate_start_index")
    return EndpointResolutionRecord(
        decision_id=str(decision_id)[:_MAX_VALUE],
        session_id=str(session_id)[:_MAX_VALUE] if session_id else None,
        model=str(event.get("model") or "")[:_MAX_VALUE],
        selected=(str(event.get("selected"))[:_MAX_VALUE] if event.get("selected") else None),
        provider=(str(event.get("provider"))[:_MAX_VALUE] if event.get("provider") else None),
        subscription_class=(
            str(event.get("subscription_class"))[:_MAX_VALUE]
            if event.get("subscription_class")
            else None
        ),
        executable_binding=executable_binding,
        skipped=skipped,
        observed_at=observed_at or datetime.now(UTC).isoformat(),
        balance_policy=(str(raw_balance)[:_MAX_VALUE] if raw_balance else None),
        rotate_start_index=(int(raw_start) if isinstance(raw_start, int) else None),
    )


def latest_resolution_for_decision(
    decision_id: str, path: Path | str | None = None, model: str | None = None
) -> EndpointResolutionRecord | None:
    """Return the newest valid resolution record for a decision.

    A decision can resolve several models (one per explicit-endpoint role), so
    a caller attributing a failure passes the model it concerns; without one
    the newest record for the decision is returned whatever model it names.
    """
    target = endpoint_resolution_path() if path is None else Path(path)
    records = _read_decision_history_unbounded(target)
    for raw in reversed(records):
        if raw.get("decision_id") != decision_id:
            continue
        if model is not None and raw.get("model") != model:
            continue
        try:
            return EndpointResolutionRecord(
                decision_id=str(raw.get("decision_id") or ""),
                session_id=raw.get("session_id"),
                model=str(raw.get("model") or ""),
                selected=raw.get("selected"),
                provider=raw.get("provider"),
                subscription_class=raw.get("subscription_class"),
                executable_binding=raw.get("executable_binding"),
                skipped=tuple(raw.get("skipped") or ()),
                observed_at=str(raw.get("observed_at") or ""),
                schema=str(raw.get("schema") or ENDPOINT_RESOLUTION_SCHEMA),
                balance_policy=raw.get("balance_policy"),
                rotate_start_index=raw.get("rotate_start_index"),
            )
        except (TypeError, ValueError):
            continue
    return None


def persist_endpoint_resolution(
    record: EndpointResolutionRecord, path: Path | str | None = None
) -> None:
    """Append one locked, redacted endpoint-resolution-v1 record unless unchanged."""
    target = endpoint_resolution_path() if path is None else Path(path)
    previous = latest_resolution_for_decision(record.decision_id, target, model=record.model)
    if previous is not None:
        old = previous.to_dict()
        new = record.to_dict()
        old.pop("observed_at", None)
        new.pop("observed_at", None)
        if old == new:
            return
    append_jsonl(target, record.to_dict())


__all__ = [
    "latest_resolution_for_decision",
    "ENDPOINT_RESOLUTION_SCHEMA",
    "EndpointResolutionRecord",
    "endpoint_resolution_path",
    "make_endpoint_resolution",
    "persist_endpoint_resolution",
]
