"""Bounded operational registry for semantic-judge instrument health."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from grokbuild.endpoint_resolution import EndpointResolutionRecord
from grokbuild.persist import append_jsonl, iter_jsonl, sidecar_generations, sidecar_path

JUDGE_HEALTH_SCHEMA = "judge-health-v1"
MAX_VALUE = 500


def _text(value: object) -> str:
    return str(value or "")[:MAX_VALUE]


@dataclass(frozen=True)
class JudgeHealthRecord:
    health_id: str
    provider: str
    endpoint: str
    requested_model: str
    resolved_model: str
    prompt_hash: str
    rubric_version: str
    temperature: float
    reasoning_effort: str
    response_schema: str
    canary_battery_version: str
    repeatability: float
    position_bias: float
    observed_at: str = ""
    schema: str = JUDGE_HEALTH_SCHEMA

    def __post_init__(self) -> None:
        required = (
            self.health_id,
            self.provider,
            self.endpoint,
            self.requested_model,
            self.resolved_model,
            self.prompt_hash,
            self.rubric_version,
            self.reasoning_effort,
            self.response_schema,
            self.canary_battery_version,
        )
        if not all(required):
            raise ValueError("judge health instrument metadata must be complete")
        if not 0.0 <= self.repeatability <= 1.0 or not 0.0 <= self.position_bias <= 1.0:
            raise ValueError("judge health measurements must be within 0..1")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in tuple(payload.items()):
            if isinstance(value, str):
                payload[key] = _text(value)
        payload["observed_at"] = self.observed_at or datetime.now(UTC).isoformat()
        return payload


def from_endpoint_resolution(
    *,
    health_id: str,
    resolution: EndpointResolutionRecord,
    prompt_hash: str,
    rubric_version: str,
    temperature: float,
    reasoning_effort: str,
    response_schema: str,
    canary_battery_version: str,
    repeatability: float,
    position_bias: float,
) -> JudgeHealthRecord:
    binding = resolution.executable_binding or {}
    return JudgeHealthRecord(
        health_id=health_id,
        provider=resolution.provider or "unknown",
        endpoint=str(binding.get("base_url") or resolution.selected or "unknown"),
        requested_model=resolution.model,
        resolved_model=str(binding.get("model") or resolution.selected or "unknown"),
        prompt_hash=prompt_hash,
        rubric_version=rubric_version,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        response_schema=response_schema,
        canary_battery_version=canary_battery_version,
        repeatability=repeatability,
        position_bias=position_bias,
    )


def judge_health_path() -> Path:
    return sidecar_path("judge-health-v1.jsonl")


def record_judge_health(record: JudgeHealthRecord, path: Path | str | None = None) -> None:
    append_jsonl(judge_health_path() if path is None else path, record.to_dict())


def read_judge_health(path: Path | str | None = None) -> Iterator[dict[str, Any]]:
    target = judge_health_path() if path is None else Path(path)
    for generation in sidecar_generations(target)[::-1]:
        if not generation.is_file():
            continue
        for _, raw in iter_jsonl(generation):
            if raw.get("schema") == JUDGE_HEALTH_SCHEMA:
                yield dict(raw)


def delta_below_instrument_noise(delta: float, records: tuple[JudgeHealthRecord, ...]) -> bool:
    """Flag selector deltas no larger than measured disagreement and bias."""
    if not records:
        return True
    noise = max(max(1.0 - item.repeatability, item.position_bias) for item in records)
    return abs(float(delta)) <= noise


__all__ = [
    "JUDGE_HEALTH_SCHEMA",
    "JudgeHealthRecord",
    "delta_below_instrument_noise",
    "from_endpoint_resolution",
    "judge_health_path",
    "read_judge_health",
    "record_judge_health",
]
