from __future__ import annotations

from pathlib import Path

import grokbuild.settlement as settlement
from grokbuild.evidence import FailureSignal
from grokbuild.state import RuntimeState, default_state_path, load_state
from grokbuild.transactions import (
    allocate_parallel_member_tx,
    bind_parallel_member_task_tx,
    ensure_execution_tx,
    record_session_debt_stage_tx,
)

SID = "01a0063b-a2dd-7bf2-aafd-4a66cf8377cc"
BARRIER = [
    {
        "stage_id": "recon",
        "role": "recon",
        "required": True,
        "kind": "parallel_spawn",
        "members": [
            {"member_id": "0", "role": "explore", "required": True},
            {"member_id": "1", "role": "explore", "required": True},
        ],
    }
]
ONE_MEMBER_BARRIER = [
    {
        "stage_id": "recon",
        "role": "recon",
        "required": True,
        "kind": "parallel_spawn",
        "members": [
            {"member_id": "0", "role": "explore", "required": True},
        ],
    }
]


def _setup(tmp_path: Path, monkeypatch, barrier=BARRIER) -> Path:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = default_state_path()
    ensure_execution_tx(path, "decision", SID, 1, barrier)
    return path


def test_debt_environment_failure_does_not_open_circuit(tmp_path: Path, monkeypatch) -> None:
    path = _setup(tmp_path, monkeypatch, ONE_MEMBER_BARRIER)
    assert allocate_parallel_member_tx(path, "decision", "explore") == "recon/0"
    result = record_session_debt_stage_tx(
        path,
        SID,
        None,
        "explore",
        False,
        3600,
        threshold=1,
        failure_signal=FailureSignal(text="no space left on device"),
    )
    assert result == "decision"
    state = load_state(path)
    assert "explore" not in state.session_circuits.get(SID, {})


def test_debt_non_environment_failure_opens_circuit(tmp_path: Path, monkeypatch) -> None:
    path = _setup(tmp_path, monkeypatch, ONE_MEMBER_BARRIER)
    assert allocate_parallel_member_tx(path, "decision", "explore") == "recon/0"
    record_session_debt_stage_tx(
        path,
        SID,
        None,
        "explore",
        False,
        3600,
        threshold=1,
        failure_signal=FailureSignal(text="model failure"),
    )
    state = load_state(path)
    assert state.session_circuits[SID]["explore"].circuit_open_until > 0


def test_from_dict_quarantines_corrupt_turn_bookkeeping() -> None:
    state = RuntimeState.from_dict(
        {
            "turns": {"valid": 4, "bad": "not-an-int"},
            "turn_seen": {"valid": 12.5, "bad": "not-a-float"},
        }
    )
    assert state.turns == {"valid": 4}
    assert state.turn_seen == {"valid": 12.5}


def test_owed_result_key_chooses_earliest_requested_member(tmp_path: Path, monkeypatch) -> None:
    path = _setup(tmp_path, monkeypatch)
    first = allocate_parallel_member_tx(path, "decision", "explore")
    second = allocate_parallel_member_tx(path, "decision", "explore")
    assert (first, second) == ("recon/0", "recon/1")
    bind_parallel_member_task_tx(path, "decision", "explore", "task-0")
    bind_parallel_member_task_tx(path, "decision", "explore", "task-1")
    track = load_state(path).get_execution("decision")
    assert track is not None
    track.requested_at[first] = 20.0
    track.requested_at[second] = 30.0
    assert settlement._owed_result_key(track, "explore") == first
