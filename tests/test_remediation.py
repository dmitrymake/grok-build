from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from grokbuild import hook, remediation
from grokbuild.classify import load_intents
from grokbuild.decision import ExecutionStage
from grokbuild.remediation import (
    compact_remediation_debt,
    open_remediation_debt,
    open_remediation_debts,
    record_remediation_attempt,
    remediation_path,
    remediation_stop_context,
)
from grokbuild.state import load_state
from grokbuild.transactions import (
    bind_linear_stage_task_tx,
    ensure_execution_tx,
    record_retrieval_result_tx,
    record_stage_tx,
)
import grokbuild.transactions as transactions


def _hint(reversibility: str = "reversible") -> dict[str, str]:
    return {
        "kind": "rename",
        "description": "Rename the data before deletion.",
        "reversibility": reversibility,
        "operator_context": "Confirm rollback evidence.",
    }


def _open(path: Path, *, now: float = 100.0, reversibility: str = "reversible"):
    return open_remediation_debt(
        session_id="session-1",
        decision_id="decision-1",
        reason_code="zero_write",
        current_branch="implement-hard",
        safe_alternative=_hint(reversibility),
        path=path,
        now=now,
    )


def test_malformed_middle_record_is_quarantined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "remediation.jsonl"
    first = _open(path, now=100)
    assert first is not None
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{torn\\n")
    second = _open(path, now=110)
    assert second is not None
    debts = open_remediation_debts("session-1", 150, path=path, now=120)
    assert len(debts) == 1
    assert debts[0].obligation == second.obligation
    assert not debts[0].resolved
    assert "quarantined invalid remediation history" in capsys.readouterr().err


def test_cross_turn_creation_and_update(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    created = _open(path)
    assert created is not None
    assert open_remediation_debts("session-1", 150, path=path, now=200) == (created,)

    assert (
        record_remediation_attempt(
            session_id="session-1",
            decision_id="decision-1",
            branch="implement-hard",
            success=False,
            evidence_references=("evidence:decision-1:implement-hard",),
            path=path,
            now=220,
        )
        == 1
    )
    updated = open_remediation_debts("session-1", 150, path=path, now=221)[0]
    assert updated.attempt_outcomes[0]["outcome"] == "failure"
    assert updated.evidence_references == ("evidence:decision-1:implement-hard",)
    assert updated.unresolved_operations


def test_success_reconciles_obligation(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    _open(path)
    assert (
        record_remediation_attempt(
            session_id="session-1",
            decision_id="decision-1",
            branch="implement-hard",
            success=True,
            path=path,
            now=120,
        )
        == 1
    )
    assert open_remediation_debts("session-1", 500, path=path, now=121) == ()


def test_obligation_reconciles_across_decision_ids_without_duplicate_debt(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    first = _open(path, now=100)
    assert first is not None

    reopened = open_remediation_debt(
        session_id="session-1",
        decision_id="decision-2",
        reason_code="zero_write",
        current_branch="implement-hard",
        safe_alternative=_hint(),
        path=path,
        now=110,
    )
    assert reopened is not None
    assert reopened.obligation == first.obligation
    assert reopened.decision_references == ("decision-1", "decision-2")
    assert len(open_remediation_debts("session-1", 500, path=path, now=111)) == 1

    assert (
        record_remediation_attempt(
            session_id="session-1",
            decision_id="decision-2",
            branch="implement-hard",
            success=True,
            path=path,
            now=120,
        )
        == 1
    )
    assert open_remediation_debts("session-1", 500, path=path, now=121) == ()


def test_expiry_uses_debt_window(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    _open(path, now=100)
    assert open_remediation_debts("session-1", 50, path=path, now=150)
    assert open_remediation_debts("session-1", 50, path=path, now=151) == ()


def test_compaction_respects_profile_debt_window(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    _open(path, now=0)
    assert compact_remediation_debt(path, now=108000, debt_window_seconds=172800) == (1, 1)
    assert compact_remediation_debt(path, now=108000, debt_window_seconds=86400) == (1, 0)


def test_attempt_compaction_resolves_active_profile_window(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "remediation.jsonl"
    record = _open(path, now=0)
    assert record is not None
    with path.open("a", encoding="utf-8") as stream:
        for _ in range(399):
            stream.write(json.dumps(record.to_dict()) + "\n")
    monkeypatch.setattr(
        remediation,
        "load_profiles",
        lambda: {"default": SimpleNamespace(debt_window_seconds=172800.0)},
    )
    assert (
        record_remediation_attempt(
            session_id="session-1",
            decision_id="decision-1",
            branch="implement-hard",
            success=False,
            path=path,
            now=108000,
        )
        == 1
    )
    assert open_remediation_debts("session-1", 172800, path=path, now=108000)


def test_compaction_boundary_uses_max_ttl(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    _open(path, now=0)
    assert compact_remediation_debt(path, now=172800, debt_window_seconds=172800) == (1, 1)


def test_duplicate_attempt_is_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    _open(path)
    kwargs = {
        "session_id": "session-1",
        "decision_id": "decision-1",
        "branch": "implement-hard",
        "success": False,
        "path": path,
        "now": 120,
    }
    assert record_remediation_attempt(**kwargs) == 1
    assert record_remediation_attempt(**kwargs) == 0
    record = open_remediation_debts("session-1", 500, path=path, now=121)[0]
    assert len(record.attempt_outcomes) == 1


def test_sidecar_is_redaction_safe(tmp_path: Path) -> None:
    path = tmp_path / "remediation.jsonl"
    hint = _hint()
    hint["description"] = "Rename with token=supersecret"
    open_remediation_debt(
        session_id="session-1",
        decision_id="decision-1",
        reason_code="zero_write",
        current_branch="implement-hard",
        safe_alternative=hint,
        path=path,
        now=100,
    )
    raw = path.read_text(encoding="utf-8")
    assert "supersecret" not in raw
    assert "[redacted]" in raw


def test_denial_seam_opens_remediation_debt(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: None)

    with pytest.raises(SystemExit):
        hook._finish_pre_tool(
            "deny",
            "zero_write",
            detail="blocked",
            session_id="session-1",
            decision_id="decision-1",
            next_stage="implement-hard",
            gate_active=True,
        )

    capsys.readouterr()
    debts = open_remediation_debts("session-1", 10, now=time.time())
    assert len(debts) == 1
    assert debts[0].current_branch == "implement-hard"


def test_child_result_updates_and_reconciles_debt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _open(remediation_path(), now=time.time())
    state_path = tmp_path / "runtime" / "state.json"
    stage = ExecutionStage("implement-hard", True, "implementation")
    ensure_execution_tx(state_path, "decision-1", "session-1", 1, (stage.to_dict(),))

    record_stage_tx(
        state_path,
        "decision-1",
        "result",
        role="implement-hard",
        success=False,
    )
    failed = open_remediation_debts("session-1", 10, now=time.time())[0]
    assert failed.attempt_outcomes[-1]["outcome"] == "failure"
    assert failed.evidence_references

    record_stage_tx(
        state_path,
        "decision-1",
        "result",
        role="implement-hard",
        success=True,
    )
    assert open_remediation_debts("session-1", 10, now=time.time()) == ()


@pytest.mark.parametrize("use_retrieval", [False, True])
@pytest.mark.parametrize("failing_io", ["append", "evidence"])
def test_repair_side_effects_are_deferred_until_state_commit(
    tmp_path: Path, monkeypatch, use_retrieval: bool, failing_io: str
) -> None:
    state_path = tmp_path / "runtime" / "state.json"
    stage = ExecutionStage("implement-hard", True, "implementation")
    ensure_execution_tx(state_path, "decision-1", "session-1", 1, (stage.to_dict(),))
    if use_retrieval:
        record_stage_tx(state_path, "decision-1", "requested", role="implement-hard")
        bind_linear_stage_task_tx(state_path, "decision-1", "implement-hard", "task-1")
    monkeypatch.setattr("grokbuild.compose.compose_consilium_barrier", lambda _registry: None)
    attempted: list[str] = []

    def append_stub(*_args, **_kwargs):
        attempted.append("append")
        if failing_io == "append":
            raise OSError("append failed")

    def evidence_stub(*_args, **_kwargs):
        attempted.append("evidence")
        if failing_io == "evidence":
            raise OSError("evidence failed")

    monkeypatch.setattr(transactions, "append_jsonl", append_stub)
    monkeypatch.setattr(transactions, "persist_evidence", evidence_stub)
    if use_retrieval:

        def operation() -> None:
            record_retrieval_result_tx(
                state_path,
                "session-1",
                "decision-1",
                "task-1",
                False,
                consilium_after_failures=1,
            )

    else:

        def operation() -> None:
            record_stage_tx(
                state_path,
                "decision-1",
                "result",
                role="implement-hard",
                success=False,
                consilium_after_failures=1,
            )

    with pytest.raises(OSError):
        operation()
    track = load_state(state_path).get_execution("decision-1")
    assert track is not None
    assert "consilium-unavailable" in {stage.stage_id for stage in track.stages}
    assert attempted == (["append"] if failing_io == "append" else ["append", "evidence"])


@pytest.mark.parametrize(
    ("reversibility", "expected_action"),
    [
        ("reversible", "continue_safe_remediation"),
        ("irreversible", "BLOCKED+escalate"),
    ],
)
def test_stop_surfaces_calibrated_remediation_policy(
    tmp_path: Path, monkeypatch, reversibility: str, expected_action: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    record = _open(remediation_path(), now=time.time(), reversibility=reversibility)
    assert record is not None
    telemetry: list[dict[str, object]] = []
    blocked: list[str] = []
    route = {
        "mode": "dynamic",
        "decision_id": "current-decision",
        "has_strong": False,
        "write_policy": "observe",
        "role_spawnable": True,
        "execution_valid": True,
        "warnings": [],
    }
    monkeypatch.setattr(hook, "resolve_route", lambda *args, **kwargs: route)
    monkeypatch.setattr(hook, "_sweep_pending_children", lambda *args: (0, 0, 0, 0, 0))
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: telemetry.append(payload))
    monkeypatch.setattr(hook, "_block_stop", lambda detail: blocked.append(detail))

    hook.handle_stop(
        {"hookEventName": "Stop", "sessionId": "session-1", "reason": "end_turn"},
        load_intents(),
    )

    stop = next(item for item in telemetry if item.get("event") == "stop")
    assert stop["blocked"] is True
    assert stop["reason_code"] == "remediation_debt"
    assert stop["remediation_debt"]["action"] == expected_action
    assert blocked
    if reversibility != "irreversible":
        assert "BLOCKED+escalate" not in json.dumps(stop)


def test_unknown_reversibility_never_escalates_as_irreversible(tmp_path: Path) -> None:
    record = _open(tmp_path / "remediation.jsonl", reversibility="unknown")
    assert record is not None
    assert remediation_stop_context(record)["action"] == "hold_for_evidence"
