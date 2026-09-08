#!/usr/bin/env python3
"""Regression tests for the parallel-barrier gate livelock.

A required barrier member whose binding is released after two ``not_found``
retrievals used to lose its task correlation entirely: ``member_tasks`` was
popped without recording ``task_id -> stage_key`` anywhere, so a late
successful retrieval for the same task id matched nothing. The member stayed
in ``failed``, ``incomplete_required_members`` never emptied, the barrier never
closed, and the gate denied every following stage with ``wrong_stage`` while
the Stop hook kept prescribing another spawn.
"""

from __future__ import annotations

from _harness import setup_environment

from _harness import plant_session

from _harness import run_hook_main

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import time  # noqa: E402

from grokbuild import gate  # noqa: E402
from grokbuild.decision import ExecutionStage  # noqa: E402
from grokbuild.state import (
    ExecutionTrack,
    default_state_path,
    load_state,
    pending_child_bindings,
    save_state,
)
from grokbuild.transactions import (
    allocate_parallel_member_tx,
    bind_debt_stage_task_tx,
    bind_linear_stage_task_tx,
    bind_parallel_member_task_tx,
    bind_terminal_task_tx,
    bind_verify_task_tx,
    ensure_execution_tx,
    record_retrieval_result_tx,
    record_review_inconclusive_tx,
    record_stage_tx,
    release_unresolvable_binding_tx,
    resolve_task_binding,
)

from _harness import SID

FAILURES: list[str] = []


check = make_check(FAILURES)

RECON_BARRIER = [
    {
        "stage_id": "recon",
        "role": "recon",
        "required": True,
        "kind": "parallel_spawn",
        "members": [
            {"member_id": "0", "role": "explore", "required": True, "reason": "livelock"},
            {"member_id": "1", "role": "explore", "required": True, "reason": "livelock"},
        ],
    }
]


def _barrier(track: ExecutionTrack) -> ExecutionStage:
    return next(stage for stage in track.stages if stage.stage_id == "recon")


def _open_members(path: Path, decision_id: str) -> list[str]:
    track = load_state(path).get_execution(decision_id)
    assert track is not None
    stage = _barrier(track)
    return [
        ExecutionTrack.member_key(stage.stage_id, member.member_id)
        for member in track.incomplete_required_members(stage)
    ]


def test_linear_binding_not_found_streak_releases_and_keeps_tombstone(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "linear-not-found-decision"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [{"stage_id": "explore", "role": "explore", "required": True}],
    )
    record_stage_tx(path, decision_id, "requested", role="explore")
    bind_linear_stage_task_tx(path, decision_id, "explore", "task-linear")
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-linear") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-linear") == "released"
    track = load_state(path).get_execution(decision_id)
    assert track is not None
    assert track.stage_tasks == {}
    assert track.terminal_tasks == {"task-linear": "explore"}
    assert track.failed == ["explore"]
    assert track.failure_reasons["explore"] == "task_not_found"
    record_stage_tx(path, decision_id, "requested", role="explore", task_id="task-linear")
    assert bind_linear_stage_task_tx(path, decision_id, "explore", "task-linear") == "explore"
    assert pending_child_bindings(load_state(path), SID) == ["task-linear"]
    assert resolve_task_binding(path, SID, decision_id, "task-linear") == (decision_id, "explore")
    assert record_retrieval_result_tx(path, SID, decision_id, "task-linear", True) == "recorded"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.completed == ["explore"] and not track.failed
    assert not track.terminal_tasks


def test_verify_binding_not_found_streak_releases_and_can_rebind(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "verify-not-found-decision"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [{"stage_id": "verify/0", "role": "verify/0", "required": True, "kind": "verify"}],
    )
    assert bind_verify_task_tx(path, decision_id, "verify-task", "verify/0") == "verify/0"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "verify-task") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "verify-task") == "released"
    track = load_state(path).get_execution(decision_id)
    assert track is not None
    assert not track.verify_tasks
    assert track.failed == ["verify/0"]
    assert track.failure_reasons["verify/0"] == "task_not_found"
    assert bind_verify_task_tx(path, decision_id, "verify-retry", "verify/0") == "verify/0"


def test_fanout_verify_not_found_releases_every_track(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    stages = [{"stage_id": "verify/0", "role": "verify/0", "required": True, "kind": "verify"}]
    ensure_execution_tx(path, "verify-fanout-a", SID, 1, stages)
    ensure_execution_tx(path, "verify-fanout-b", SID, 1, stages)
    assert bind_verify_task_tx(path, "verify-fanout-a", "verify-task", "verify/0") == "verify/0"
    assert bind_verify_task_tx(path, "verify-fanout-b", "verify-task", "verify/0") == "verify/0"
    assert release_unresolvable_binding_tx(path, SID, "verify-fanout-a", "verify-task") == "streak"
    assert (
        release_unresolvable_binding_tx(path, SID, "verify-fanout-a", "verify-task") == "released"
    )
    for decision_id in ("verify-fanout-a", "verify-fanout-b"):
        track = load_state(path).get_execution(decision_id)
        assert track is not None and not track.verify_tasks
        assert track.failure_reasons["verify/0"] == "task_not_found"
        assert track.released_verify_tasks == {"verify-task": "verify/0"}


def test_stale_not_found_does_not_drop_rebound_linear_task(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stale-not-found-decision"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [{"stage_id": "explore", "role": "explore", "required": True}],
    )
    record_stage_tx(path, decision_id, "requested", role="explore")
    assert bind_linear_stage_task_tx(path, decision_id, "explore", "old-task") == "explore"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "old-task") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "old-task") == "released"
    record_stage_tx(path, decision_id, "requested", role="explore", task_id="new-task")
    assert bind_linear_stage_task_tx(path, decision_id, "explore", "new-task") == "explore"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "old-task") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "old-task") == "streak"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.stage_tasks == {"explore": "new-task"}


def test_terminal_binding_not_found_streak_releases_and_keeps_tombstone(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "terminal-not-found-decision"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [{"stage_id": "explore", "role": "explore", "required": True}],
    )
    bind_terminal_task_tx(path, decision_id, "task-terminal", "explore")
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-terminal") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-terminal") == "released"
    track = load_state(path).get_execution(decision_id)
    assert track is not None
    assert track.terminal_tasks == {"task-terminal": "explore"}
    assert track.failure_reasons["explore"] == "task_not_found"


def test_new_binding_not_found_streak_releases_after_two_lookups(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "streak-reset-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    assert key == "recon/0"
    bind_parallel_member_task_tx(path, decision_id, "explore", "task-a")
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-a") == "streak"
    assert record_stage_tx(path, decision_id, "result", role=key, success=False) is None
    track = load_state(path).get_execution(decision_id)
    assert track is not None and key not in track.not_found_streaks
    assert allocate_parallel_member_tx(path, decision_id, "explore") == key
    bind_parallel_member_task_tx(path, decision_id, "explore", "task-b")
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-b") == "streak"
    assert release_unresolvable_binding_tx(path, SID, decision_id, "task-b") == "released"


def test_stale_linear_failure_does_not_steal_replacement_binding(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stale-linear-replacement"
    ensure_execution_tx(
        path, decision_id, SID, 1, [{"role": "explore", "required": True, "kind": "spawn"}]
    )
    record_stage_tx(path, decision_id, "requested", role="explore")
    bind_linear_stage_task_tx(path, decision_id, "explore", "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    record_stage_tx(path, decision_id, "requested", role="explore")
    bind_linear_stage_task_tx(path, decision_id, "explore", "B")
    before = load_state(path).get_execution(decision_id).to_dict()
    assert record_retrieval_result_tx(path, SID, decision_id, "A", success=False) == "recorded"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.stage_tasks == {"explore": "B"}
    assert track.repair_failures == before["repair_failures"]
    assert record_retrieval_result_tx(path, SID, decision_id, "B", success=True) == "recorded"
    assert "explore" in load_state(path).get_execution(decision_id).completed


def test_stale_review_inconclusive_does_not_steal_replacement_binding(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stale-review-replacement"
    ensure_execution_tx(
        path, decision_id, SID, 1, [{"role": "review-hard", "required": True, "kind": "spawn"}]
    )
    record_stage_tx(path, decision_id, "requested", role="review-hard")
    bind_linear_stage_task_tx(path, decision_id, "review-hard", "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    record_stage_tx(path, decision_id, "requested", role="review-hard")
    bind_linear_stage_task_tx(path, decision_id, "review-hard", "B")
    assert record_review_inconclusive_tx(path, SID, decision_id, "A") == "recorded"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.stage_tasks == {"review-hard": "B"}
    assert "review_inconclusive:review-hard" not in track.warnings


def test_stale_parallel_failure_does_not_steal_replacement_binding(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stale-parallel-replacement"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    release_unresolvable_binding_tx(path, SID, decision_id, "A")
    allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "B")
    before = load_state(path).get_execution(decision_id).to_dict()
    assert record_retrieval_result_tx(path, SID, decision_id, "A", success=False) == "recorded"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and track.member_tasks.get(key) == "B"
    assert track.repair_failures == before["repair_failures"]
    assert record_retrieval_result_tx(path, SID, decision_id, "B", success=True) == "recorded"
    assert key in load_state(path).get_execution(decision_id).completed


def test_late_success_after_release_closes_the_barrier(tmp: Path) -> None:
    """The livelock repro: released member + late success must close the barrier."""
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "livelock-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    check(released == "recon/0", "first explore shard is allocated and bound")

    sibling = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T1")
    record_retrieval_result_tx(path, SID, decision_id, "T1", success=True)
    check(sibling == "recon/1", "second explore shard is allocated and bound")
    check(
        _open_members(path, decision_id) == [released],
        "sibling completion leaves only the first shard open",
    )

    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "T0") == "streak",
        "first not_found keeps the binding",
    )
    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "T0") == "released",
        "second not_found releases the binding",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released not in track.member_tasks and released in track.failed,
        "released member is failed and unbound",
    )

    # The bug: this late success used to match nothing, leaving the member failed.
    check(
        record_retrieval_result_tx(path, SID, decision_id, "T0", success=True) == "recorded",
        "late success for the released task id is still correlated",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released in track.completed and released not in track.failed,
        "late success moves the released member from failed to completed",
    )
    check(
        _open_members(path, decision_id) == [],
        "barrier closes: no incomplete required members remain",
    )
    check(
        track is not None and "T0" not in track.terminal_tasks,
        "resolved correlation is cleaned up and does not accumulate",
    )


def test_late_failure_after_release_keeps_member_failed(tmp: Path) -> None:
    """A late *failed* retrieval must not resurrect the member as completed."""
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "late-failure-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")

    check(
        record_retrieval_result_tx(path, SID, decision_id, "T0", success=False) == "recorded",
        "late failure for the released task id is correlated",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released in track.failed and released not in track.completed,
        "late failure leaves the released member failed",
    )
    check(
        track is not None and "T0" not in track.terminal_tasks,
        "late failure clears the release correlation too",
    )


def test_released_member_stays_retryable_and_unambiguous(tmp: Path) -> None:
    """Re-spawning a released member must not collide with the kept correlation."""
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "retry-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")

    check(
        allocate_parallel_member_tx(path, decision_id, "explore") == released,
        "released member is still retryable",
    )
    check(
        bind_parallel_member_task_tx(path, decision_id, "explore", "T2") == released,
        "the retry binds a fresh task id to the same member",
    )
    check(
        record_retrieval_result_tx(path, SID, decision_id, "T2", success=True) == "recorded",
        "the retry result is unambiguous despite the kept T0 correlation",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released in track.completed,
        "the retry completes the member",
    )
    check(
        record_retrieval_result_tx(path, SID, decision_id, "T0", success=True) == "recorded",
        "a later straggler for the released task id is still resolvable",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released in track.completed and "T0" not in track.terminal_tasks,
        "the straggler is absorbed without reopening or duplicating the member",
    )


def test_duplicate_ack_after_release_does_not_steal_a_sibling(tmp: Path) -> None:
    """A duplicate spawn ack for a released task id must stay on its own member."""
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "duplicate-ack-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")

    # The executor re-spawns the released shard with a fresh id, then moves on to
    # the sibling, which is requested but not yet acknowledged.
    allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T2")
    sibling = allocate_parallel_member_tx(path, decision_id, "explore")
    check(sibling == "recon/1", "the sibling shard is allocated next")

    check(
        bind_parallel_member_task_tx(path, decision_id, "explore", "T0") == released,
        "a duplicate ack for the released task id resolves to its original member",
    )
    check(
        record_retrieval_result_tx(path, SID, decision_id, "T0", success=True) == "recorded",
        "the duplicated task id never becomes an ambiguous match",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None
        and track.member_tasks.get(released) == "T2"
        and released not in track.completed
        and sibling not in track.completed,
        "the stale duplicate ack leaves the replacement member intact",
    )


def test_debt_binding_reuses_the_kept_correlation(tmp: Path) -> None:
    """A parked correlation counts as a binding for the session debt binder.

    Otherwise the same task id could be claimed by a second track, and every
    later lookup would see two matches, return ``ambiguous`` without mutating,
    and leave the id unresolvable for good.
    """
    setup_environment(tmp)
    path = default_state_path()
    parked_decision = "debt-parked-decision"
    ensure_execution_tx(
        path,
        parked_decision,
        SID,
        1,
        [
            {
                "stage_id": "recon",
                "role": "recon",
                "required": True,
                "kind": "parallel_spawn",
                "members": [
                    {"member_id": "0", "role": "explore", "required": True, "reason": "debt"},
                ],
            }
        ],
    )
    released = allocate_parallel_member_tx(path, parked_decision, "explore")
    bind_parallel_member_task_tx(path, parked_decision, "explore", "T0")
    release_unresolvable_binding_tx(path, SID, parked_decision, "T0")
    release_unresolvable_binding_tx(path, SID, parked_decision, "T0")
    # The retry completes the member, so this track is no longer a debt candidate
    # while its parked correlation for the abandoned id survives.
    allocate_parallel_member_tx(path, parked_decision, "explore")
    bind_parallel_member_task_tx(path, parked_decision, "explore", "T4")
    record_retrieval_result_tx(path, SID, parked_decision, "T4", success=True)
    track = load_state(path).get_execution(parked_decision)
    check(
        track is not None
        and track.all_required_completed()
        and track.terminal_tasks == {"T0": released},
        "the completed track still carries the parked correlation",
    )

    other_decision = "debt-other-decision"
    ensure_execution_tx(
        path,
        other_decision,
        SID,
        1,
        [{"role": "explore", "required": True, "kind": "spawn", "reason": "debt"}],
    )

    check(
        bind_debt_stage_task_tx(path, SID, "debt-current", "explore", "T0", 3600.0) == released,
        "the debt binder reuses the parked correlation instead of claiming a second stage",
    )
    other = load_state(path).get_execution(other_decision)
    check(
        other is not None and other.stage_tasks == {},
        "no competing binding is created in the other track",
    )
    check(
        record_retrieval_result_tx(path, SID, "debt-current", "T0", success=True) == "recorded",
        "the task id stays unambiguous across tracks",
    )


def test_kept_correlation_never_downgrades_a_live_member_binding(tmp: Path) -> None:
    """The parked correlation must not shadow a live member binding.

    Auto-release fires for member, linear, terminal, and verify bindings.
    ``record_stage_tx`` can
    re-request the same member under the same task id, so the parked correlation
    and a live member binding can coexist. If the parked one won the match, the
    member would never be releasable again and would hold the barrier open.
    """
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "second-release-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.terminal_tasks == {"T0": released},
        "the release parks exactly one correlation",
    )

    record_stage_tx(path, decision_id, "requested", role=released, task_id="T0")
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.member_tasks.get(released) == "T0",
        "the member is re-requested under the same task id",
    )

    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "T0") == "streak",
        "the re-requested member starts a fresh not_found streak",
    )
    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "T0") == "released",
        "the re-requested member is still auto-releasable",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and released not in track.member_tasks,
        "the second release unbinds the member again",
    )
    check(
        allocate_parallel_member_tx(path, decision_id, "explore") == released,
        "the member remains retryable after a second release",
    )


def run_main(payload: dict) -> tuple[int, str]:
    return run_hook_main(payload, workspace_root=REPO_ROOT)


def test_barrier_stall_survives_a_released_member(tmp: Path) -> None:
    """The stall lift is the only automatic barrier escape; failure must not kill it.

    Every failure path drops the member from ``requested``, ``member_tasks`` and
    ``requested_at`` - exactly the three markers the stall check used to demand
    from every member - so the hatch used to be disabled precisely when a member
    had failed and the barrier could no longer close on its own.
    """
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stall-decision"
    ensure_execution_tx(path, decision_id, SID, 1, RECON_BARRIER)
    route = {"profile": "default", "intent": "implement"}

    released = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    pending = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T1")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")
    release_unresolvable_binding_tx(path, SID, decision_id, "T0")

    track = load_state(path).get_execution(decision_id)
    stage = _barrier(track)
    members = gate._enforceable_barrier_members(track, stage)
    check(
        {ExecutionTrack.member_key(stage.stage_id, m.member_id) for m in members}
        == {released, pending},
        "both required members are still enforceable",
    )

    stalled, age = gate._barrier_stall(track, stage, route, now=time.time() + 10.0)
    check(stalled is False and age is not None, "a fresh barrier is not stalled yet")

    stalled, age = gate._barrier_stall(track, stage, route, now=time.time() + 7200.0)
    check(
        stalled is True and age is not None and age > 1800.0,
        "an aged barrier with a released member still reports a stall",
    )


UNAVAILABLE_BARRIER = [
    {
        "stage_id": "recon",
        "role": "recon",
        "required": True,
        "kind": "parallel_spawn",
        "members": [
            {"member_id": "0", "role": "explore", "required": True, "reason": "recon"},
            {
                "member_id": "1",
                "role": "review-independent",
                "required": True,
                "reason": "independent lens",
            },
        ],
    }
]


def test_barrier_stall_waits_out_an_unavailable_member(tmp: Path) -> None:
    """A never-spawned member with an unverified role stalls on the bounded clock.

    ``review-independent`` needs a live-session availability signal the test
    environment never provides, so it can never grow the
    requested/member_tasks/requested_at triple on its own. The stall check must
    still count it (bounded wait via the track timestamp fallback) instead of
    skipping it instantly, and an explicit old ``requested_at`` entry for that
    member must drive the stall itself.
    """
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "stall-unavailable"
    ensure_execution_tx(path, decision_id, SID, 1, UNAVAILABLE_BARRIER)
    route = {"profile": "default", "intent": "implement"}

    check(
        gate._role_signal_unverified("review-independent"),
        "premise: review-independent has no verified live-session signal here",
    )
    check(
        not gate._role_signal_unverified("explore"),
        "premise: explore needs no availability signal",
    )

    allocated = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "T0")
    check(allocated == "recon/0", "the explore member is allocated and bound")

    track = load_state(path).get_execution(decision_id)
    stage = _barrier(track)
    members = gate._enforceable_barrier_members(track, stage)
    check(
        {member.role for member in members} == {"explore", "review-independent"},
        "both members stay enforceable; the unavailable one is not skipped",
    )

    stalled, age = gate._barrier_stall(track, stage, route, now=time.time() + 10.0)
    check(
        stalled is False and age is not None,
        "a fresh barrier with a never-spawned unavailable member is not stalled yet",
    )

    stalled, age = gate._barrier_stall(track, stage, route, now=time.time() + 7200.0)
    check(
        stalled is True and age is not None and age > 1800.0,
        "an aged never-spawned unavailable member still reports a bounded stall",
    )

    state = load_state(path)
    tracked = state.get_execution(decision_id)
    tracked.requested_at[ExecutionTrack.member_key(stage.stage_id, "1")] = time.time() - 2000.0
    save_state(state, path)
    tracked = load_state(path).get_execution(decision_id)
    stalled, age = gate._barrier_stall(tracked, stage, route, now=time.time() + 10.0)
    check(
        stalled is True and age is not None and age > 1800.0,
        "an explicit old requested_at drives the stall for the unavailable member",
    )


def test_sentinel_stage_is_never_prescribed_as_a_spawn(tmp: Path) -> None:
    """A sentinel role cannot be spawned, so the gate must not ask for it.

    ``_next_required_spawn_stage`` returns a required sentinel unconditionally.
    Routing it through the linear stage recipe told the agent to spawn the
    unspawnable ``consilium-unavailable`` role, which every following PreToolUse
    then denied as ``wrong_stage`` - unfollowable guidance in a closed loop.
    """
    setup_environment(tmp)
    plant_session(tmp / "grok", "route=implement: authentication code")
    run_main(
        {
            "hookEventName": "UserPromptSubmit",
            "sessionId": SID,
            "prompt": "route=implement: authentication code",
        }
    )
    path = default_state_path()
    state = load_state(path)
    decision_id, track = next(iter(state.executions.items()))
    reason = "consilium unavailable: no independent reviewer role is installed"
    track.stages = track.stages + (
        ExecutionStage(
            role="consilium-unavailable",
            required=True,
            reason=reason,
            kind="sentinel",
            stage_id="consilium-unavailable",
            slot="consilium",
        ),
    )
    save_state(state, path)

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore", "prompt": "recon"},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out,
        f"a sentinel stage still denies the spawn ({rc})",
    )
    check(
        reason in out,
        f"the denial reports the sentinel reason, as the Stop gate does ({out!r})",
    )
    check(
        "consilium-unavailable" not in out.replace(reason, ""),
        "the denial never instructs the agent to spawn the sentinel role",
    )
    check(
        "NEXT:" not in out,
        "the denial carries no unfollowable spawn recipe",
    )
    check(
        decision_id in state.executions,
        "the sentinel track is the one the gate decided on",
    )
