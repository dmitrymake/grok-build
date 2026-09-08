"""Versioned out-of-band failure-cause evidence."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Mapping

from grokbuild.persist import append_jsonl, sidecar_path

FailureCause = Literal["auth", "environment", "model", "unknown"]
EVIDENCE_SCHEMA = "evidence-v1"
_MAX_DETAIL = 500


@dataclass(frozen=True)
class FailureSignal:
    """Bounded inputs used to classify one failure."""

    text: str = ""
    reason: str = ""
    status: int | str | None = None
    provider: str = ""

    def detail(self) -> str:
        parts = (self.reason, self.status, self.provider, self.text)
        values = (str(part).strip() for part in parts if part is not None)
        return " | ".join(value for value in values if value)[:_MAX_DETAIL]


@dataclass(frozen=True)
class EvidenceRecord:
    """One evidence-v1 sidecar record."""

    decision_id: str
    session_id: str | None
    subject: Mapping[str, str]
    cause: FailureCause
    detail: str
    observed_at: str
    schema: str = EVIDENCE_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def classify_failure(signal: FailureSignal) -> FailureCause:
    """Classify a failure conservatively; unknown remains on the model path."""
    value = " ".join(
        (signal.text, signal.reason, str(signal.status or ""), signal.provider)
    ).casefold()
    if re.search(r"(?:\b401\b|\b403\b|credential|authentication|unauthori[sz]ed|forbidden)", value):
        return "auth"
    if re.search(
        r"(?:\benospc\b|not_enough_space|no space left|disk (?:full|exhaust)|"
        r"infrastructure[-_ ]timeout|timeout[-_ ]infrastructure)",
        value,
    ):
        return "environment"
    if re.search(r"(?:\b429\b|\bquota\b|model[-_ ]upstream|model[-_ ]error)", value):
        return "model"
    return "unknown"


def evidence_path() -> Path:
    return sidecar_path("evidence-v1.jsonl")


def make_evidence(
    *,
    decision_id: str,
    session_id: str | None,
    subject: Mapping[str, str],
    signal: FailureSignal,
    observed_at: str | None = None,
) -> EvidenceRecord:
    record_subject = dict(subject)
    if signal.provider:
        record_subject["provider"] = signal.provider
    return EvidenceRecord(
        decision_id=decision_id,
        session_id=session_id,
        subject=record_subject,
        cause=classify_failure(signal),
        detail=signal.detail(),
        observed_at=observed_at or datetime.now(UTC).isoformat(),
    )


def persist_evidence(record: EvidenceRecord, path: Path | str | None = None) -> None:
    """Append one locked, redacted evidence-v1 record."""
    append_jsonl(evidence_path() if path is None else path, record.to_dict())


__all__ = [
    "EVIDENCE_SCHEMA",
    "EvidenceRecord",
    "FailureCause",
    "FailureSignal",
    "classify_failure",
    "evidence_path",
    "make_evidence",
    "persist_evidence",
]
