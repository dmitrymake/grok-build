from __future__ import annotations

import json
from pathlib import Path

import pytest

from grokbuild.decision import ExecutionStage
from grokbuild.evidence import (
    EvidenceRecord,
    FailureSignal,
    classify_failure,
    evidence_path,
    is_quota_failure,
    persist_evidence,
)
from grokbuild.features import extract_features
from grokbuild.pipeline import select_implement_role
from grokbuild.roles import load_registry
from grokbuild.classify import load_intents
from grokbuild.state import RuntimeState, load_state
from grokbuild.transactions import ensure_execution_tx, record_stage_tx

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "config.toml"


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (FailureSignal(status=401), "auth"),
        (FailureSignal(reason="credential expired"), "auth"),
        (FailureSignal(text="write failed: ENOSPC"), "environment"),
        (FailureSignal(reason="timeout-infrastructure"), "environment"),
        (FailureSignal(text="model-upstream quota exhausted"), "model"),
        (FailureSignal(text="API error: Rate limit reached for requests"), "model"),
        (FailureSignal(text="BLOCKED: the rate-limit validation is incorrect"), "unknown"),
        (FailureSignal(reason="model-error"), "model"),
        (FailureSignal(text="unclassified transport failure"), "unknown"),
    ],
)
def test_failure_cause_classes(signal: FailureSignal, expected: str) -> None:
    assert classify_failure(signal) == expected


@pytest.mark.parametrize(
    "message",
    (
        "You exceeded your current quota, please check your plan and billing details.",
        "Number of requests has exceeded your per-minute rate limit.",
        "The API rate limit has been reached for this organization.",
    ),
)
def test_real_provider_quota_phrasings_are_detected(message: str) -> None:
    assert is_quota_failure(message)
    assert classify_failure(FailureSignal(text=message)) == "model"


def test_quota_diagnostic_prose_is_not_a_provider_signal() -> None:
    message = "BLOCKED: the rate-limit validation in the reviewed implementation is incorrect"
    assert not is_quota_failure(message)
    assert classify_failure(FailureSignal(text=message)) == "unknown"


def test_unknown_is_conservative_default() -> None:
    assert classify_failure(FailureSignal()) == "unknown"
    assert classify_failure(FailureSignal(text="timeout")) == "unknown"


def test_evidence_sidecar_shape_and_redaction(tmp_path: Path) -> None:
    path = tmp_path / "evidence-v1.jsonl"
    record = EvidenceRecord(
        decision_id="decision-1",
        session_id="session-1",
        subject={"role": "implement-hard"},
        cause="auth",
        detail="credential token=supersecret",
        observed_at="2026-01-01T00:00:00+00:00",
    )

    persist_evidence(record, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema",
        "decision_id",
        "session_id",
        "subject",
        "cause",
        "detail",
        "observed_at",
    }
    assert payload["schema"] == "evidence-v1"
    assert payload["detail"] == "credential [redacted]"
    assert path.stat().st_mode & 0o777 == 0o600


def test_stage_failure_attributes_environment_without_model_classification(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_path = tmp_path / "runtime" / "state.json"
    stage = ExecutionStage("implement-hard", True, "implementation")
    ensure_execution_tx(state_path, "decision-env", "session-env", 1, (stage,))

    record_stage_tx(
        state_path,
        "decision-env",
        "result",
        role="implement-hard",
        success=False,
        failure_signal=FailureSignal(text="NOT_ENOUGH_SPACE while writing output"),
    )

    records = [
        json.loads(line) for line in evidence_path().read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["cause"] == "environment"
    assert records[0]["cause"] != "model"
    assert records[0]["subject"] == {
        "stage": "implement-hard",
        "role": "implement-hard",
    }
    status = load_state(state_path).session_status_for("session-env", "implement-hard")
    assert status.consecutive_failures == 0
    assert status.total_failures == 0
    assert status.circuit_open_until == 0.0


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        ("credential expired", "auth"),
        ("ENOSPC infrastructure failure", "environment"),
        ("quota exhausted", "model"),
        ("unclassified failure", "unknown"),
    ],
)
def test_selection_failures_preserve_legacy_reason_and_record_typed_evidence(
    reason: str, cause: str
) -> None:
    registry = load_registry(REPO_CONFIG)
    state = RuntimeState()
    state.set_available("implement-hard", False, reason)
    features = extract_features("large repository migration", load_intents())
    evidence = []

    role, reasons, degraded = select_implement_role(
        "high",
        "high",
        features,
        state,
        registry,
        None,
        decision_id="decision-selection",
        evidence_records=evidence,
    )

    assert role == "implement-strong"
    assert degraded is True
    assert "skipped implement-hard (unavailable/pressured/failed)" in reasons
    assert not any("class:" in item or "infrastructure" in item for item in reasons)
    assert evidence[0].decision_id == "decision-selection"
    assert evidence[0].cause == cause


def test_auth_signal_is_not_reclassified_as_quota_or_model() -> None:
    signal = FailureSignal(status=401, reason="quota-like credential rejection")
    assert classify_failure(signal) == "auth"
