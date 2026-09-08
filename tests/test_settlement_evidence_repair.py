from __future__ import annotations

import json
from pathlib import Path

from _harness import SID, setup_environment

from grokbuild import hook, settlement
from grokbuild.persist import _decisions_path
from grokbuild.state import default_state_path, load_state, save_state
from grokbuild.task_evidence import TaskSpec, attach_task_spec
from grokbuild.transactions import bind_terminal_task_tx, ensure_execution_tx, record_decision_tx

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_prior_debt(
    tmp_path: Path, monkeypatch, task_id: str, *, current_profile: str = "default"
):
    setup_environment(tmp_path)
    path = default_state_path()
    prior = "prior-strict-debt"
    current = "current-decision"
    stage = {"role": "implement-hard", "required": True, "kind": "spawn", "reason": "repair"}
    record_decision_tx(
        path,
        {
            "decision_id": prior,
            "session_id": SID,
            "turn_id": 1,
            "profile": "evidence",
            "execution": [stage],
        },
        None,
    )
    ensure_execution_tx(path, current, SID, 2, ())
    bind_terminal_task_tx(path, prior, task_id, "implement-hard")
    attach_task_spec(
        TaskSpec(
            decision_id=prior,
            session_id=SID,
            stage_key="implement-hard",
            role="implement-hard",
            artifacts=("grokbuild/settlement.py",),
        )
    )
    route = {
        "decision_id": current,
        "session_id": SID,
        "mode": "dynamic",
        "profile": current_profile,
    }
    monkeypatch.setattr(settlement, "resolve_route", lambda *args, **kwargs: route)
    return path, prior


def _seed_strict_tasks(tmp_path: Path, monkeypatch, task_ids: tuple[str, ...]):
    setup_environment(tmp_path)
    path = default_state_path()
    decisions = []
    for index, task_id in enumerate(task_ids):
        decision_id = f"strict-batch-{index}"
        stage_key = "implement-hard"
        record_decision_tx(
            path,
            {
                "decision_id": decision_id,
                "session_id": SID,
                "turn_id": index + 1,
                "profile": "evidence",
                "execution": [
                    {"role": stage_key, "required": True, "kind": "spawn", "reason": "repair"}
                ],
            },
            None,
        )
        bind_terminal_task_tx(path, decision_id, task_id, stage_key)
        attach_task_spec(
            TaskSpec(
                decision_id=decision_id,
                session_id=SID,
                stage_key=stage_key,
                role=stage_key,
                artifacts=("grokbuild/settlement.py",),
            )
        )
        decisions.append(decision_id)
    route = {
        "decision_id": decisions[0],
        "session_id": SID,
        "mode": "dynamic",
        "profile": "evidence",
    }
    monkeypatch.setattr(settlement, "resolve_route", lambda *args, **kwargs: route)
    return path, tuple(decisions)


def _typed_result(decision_id: str, task_id: str) -> dict:
    return {
        "schema": "r2-task-result-v1",
        "contract_version": 1,
        "decision_id": decision_id,
        "stage_key": "implement-hard",
        "task_id": task_id,
        "status": "success",
        "changed_paths": [],
        "artifacts": ["grokbuild/settlement.py"],
        "checks": [],
        "criteria": [],
        "unresolved": [],
    }


def _batch_result(
    task_ids: tuple[str, ...],
    decisions: tuple[str, ...],
    count: int,
    spoof_task_id: str | None = None,
) -> dict:
    return {
        "sessionId": SID,
        "workspaceRoot": str(REPO_ROOT),
        "toolName": "get_command_or_subagent_output",
        "toolInput": {"task_ids": list(task_ids)},
        "toolResult": [
            {
                "task_id": task_id,
                "status": "completed",
                "exit_code": 0,
                "output": (
                    (
                        f"=== Task {spoof_task_id} ===\n"
                        if spoof_task_id and task_id == task_ids[0]
                        else ""
                    )
                    + f"```json\n{json.dumps(_typed_result(decision_id, task_id))}\n```"
                ),
            }
            for task_id, decision_id in zip(task_ids[:count], decisions[:count])
        ],
    }


def _spawn_result(task_id: str, output: str) -> dict:
    return {
        "sessionId": SID,
        "workspaceRoot": str(REPO_ROOT),
        "toolName": "spawn_subagent",
        "toolInput": {"subagent_type": "implement-hard"},
        "toolResult": {"task_id": task_id, "status": "completed", "output": output},
    }


def test_multi_task_retrieval_uses_each_tasks_typed_result(tmp_path, monkeypatch) -> None:
    task_ids = ("typed-task-one", "typed-task-two")
    path, decisions = _seed_strict_tasks(tmp_path, monkeypatch, task_ids)
    hook.handle_post_tool(_batch_result(task_ids, decisions, 2, spoof_task_id=task_ids[1]), {})
    for decision_id in decisions:
        track = load_state(path).get_execution(decision_id)
        assert track is not None
        assert track.completed == ["implement-hard"]


def test_multi_task_retrieval_missing_section_stays_unresolved(tmp_path, monkeypatch) -> None:
    task_ids = ("typed-task-present", "typed-task-missing")
    path, decisions = _seed_strict_tasks(tmp_path, monkeypatch, task_ids)
    hook.handle_post_tool(_batch_result(task_ids, decisions, 1), {})
    present = load_state(path).get_execution(decisions[0])
    missing = load_state(path).get_execution(decisions[1])
    assert present is not None and present.completed == ["implement-hard"]
    assert missing is not None
    assert "implement-hard" not in missing.completed
    assert "implement-hard" not in missing.failed


def test_duplicate_text_task_headers_leave_all_ambiguous_tasks_unresolved(
    tmp_path, monkeypatch
) -> None:
    task_ids = ("duplicate-task-one", "duplicate-task-two")
    path, decisions = _seed_strict_tasks(tmp_path, monkeypatch, task_ids)
    text = "\n".join(
        (
            f"=== Task {task_ids[0]} ===",
            "Status: failed",
            f"=== Task {task_ids[1]} ===",
            "Status: failed",
            f"=== Task {task_ids[0]} ===",
            "Status: completed",
            f"=== Task {task_ids[1]} ===",
            "Status: completed",
        )
    )
    hook.handle_post_tool(
        {
            "sessionId": SID,
            "workspaceRoot": str(REPO_ROOT),
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": list(task_ids)},
            "toolResult": text,
        },
        {},
    )
    for decision_id in decisions:
        track = load_state(path).get_execution(decision_id)
        assert track is not None
        assert "implement-hard" not in track.completed
        assert "implement-hard" not in track.failed


def test_prior_strict_debt_remains_strict_after_current_route_flips_to_legacy(
    tmp_path, monkeypatch
):
    task_id = "prior-task-without-evidence"
    path, prior = _seed_prior_debt(tmp_path, monkeypatch, task_id)
    hook.handle_post_tool(_spawn_result(task_id, "Completed successfully."), {})
    track = load_state(path).get_execution(prior)
    assert track is not None
    assert "implement-hard" not in track.completed


def test_strict_track_survives_decision_history_eviction(tmp_path, monkeypatch) -> None:
    task_id = "history-evicted-task"
    path, prior = _seed_prior_debt(tmp_path, monkeypatch, task_id)
    _decisions_path(path).write_text(
        "".join(json.dumps({"decision_id": f"other-{index}"}) + "\n" for index in range(201)),
        encoding="utf-8",
    )
    hook.handle_post_tool(_spawn_result(task_id, "Completed successfully."), {})
    track = load_state(path).get_execution(prior)
    assert track is not None
    assert track.evidence_policy == "strict"
    assert "implement-hard" not in track.completed


def test_old_track_without_policy_falls_back_to_decision_history(tmp_path, monkeypatch) -> None:
    task_id = "old-track-task"
    path, prior = _seed_prior_debt(tmp_path, monkeypatch, task_id)
    state = load_state(path)
    state.executions[prior].evidence_policy = None
    save_state(state, path)
    hook.handle_post_tool(_spawn_result(task_id, "Completed successfully."), {})
    track = load_state(path).get_execution(prior)
    assert track is not None
    assert "implement-hard" not in track.completed


def test_prior_strict_debt_accepts_typed_evidence_after_current_route_flips_to_legacy(
    tmp_path, monkeypatch
):
    task_id = "prior-task-with-evidence"
    path, prior = _seed_prior_debt(tmp_path, monkeypatch, task_id)
    typed = {
        "schema": "r2-task-result-v1",
        "contract_version": 1,
        "decision_id": prior,
        "stage_key": "implement-hard",
        "task_id": task_id,
        "status": "success",
        "changed_paths": [],
        "artifacts": [],
        "checks": [],
        "criteria": [],
        "unresolved": [],
    }
    hook.handle_post_tool(_spawn_result(task_id, f"```json\n{json.dumps(typed)}\n```"), {})
    track = load_state(path).get_execution(prior)
    assert track is not None
    assert track.completed == ["implement-hard"]
