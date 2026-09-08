from __future__ import annotations

import time
from pathlib import Path

from grokbuild import gate
from grokbuild.decision import ExecutionStage
from grokbuild.state import ExecutionTrack, default_state_path, load_state, save_state
from grokbuild.transactions import (
    allocate_parallel_member_tx,
    bind_parallel_member_task_tx,
    ensure_execution_tx,
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


def _barrier(track: ExecutionTrack) -> ExecutionStage:
    return next(stage for stage in track.stages if stage.stage_id == "recon")


def test_legacy_bound_member_timestamp_stalls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = default_state_path()
    decision_id = "legacy-stall"
    ensure_execution_tx(path, decision_id, SID, 1, BARRIER)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    assert key == "recon/0"
    bind_parallel_member_task_tx(path, decision_id, "explore", "task-0")
    assert allocate_parallel_member_tx(path, decision_id, "explore") == "recon/1"
    bind_parallel_member_task_tx(path, decision_id, "explore", "task-1")
    state = load_state(path)
    track = state.get_execution(decision_id)
    assert track is not None
    track.requested_at.clear()
    track.updated_at = time.time() - 2000
    save_state(state, path)

    track = load_state(path).get_execution(decision_id)
    assert track is not None
    stalled, age = gate._barrier_stall(
        track, _barrier(track), {"profile": "default"}, now=time.time()
    )
    assert stalled is True
    assert age is not None and age > 1800


def test_reclaim_only_allocation_does_not_advance_updated_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = default_state_path()
    decision_id = "reclaim-timestamp"
    ensure_execution_tx(path, decision_id, SID, 1, BARRIER)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    assert key == "recon/0"
    assert allocate_parallel_member_tx(path, decision_id, "explore") == "recon/1"
    state = load_state(path)
    track = state.get_execution(decision_id)
    assert track is not None
    old = track.updated_at
    track.member_tasks[key] = "task-0"
    track.member_tasks.pop(key)
    save_state(state, path)
    assert allocate_parallel_member_tx(path, decision_id, "explore") == key
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.updated_at == old


def test_bind_stamps_requested_at_without_clobbering_existing_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = default_state_path()
    decision_id = "bind-timestamp"
    ensure_execution_tx(path, decision_id, SID, 1, BARRIER)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    assert key == "recon/0"
    state = load_state(path)
    track = state.get_execution(decision_id)
    assert track is not None
    track.requested_at.clear()
    save_state(state, path)
    assert bind_parallel_member_task_tx(path, decision_id, "explore", "task-0") == key
    track = load_state(path).get_execution(decision_id)
    assert track is not None and key in track.requested_at
    stamp = track.requested_at[key]
    assert bind_parallel_member_task_tx(path, decision_id, "explore", "task-1") is None
    state = load_state(path)
    track = state.get_execution(decision_id)
    assert track is not None
    assert track.requested_at[key] == stamp
