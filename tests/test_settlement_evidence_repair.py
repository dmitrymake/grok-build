from __future__ import annotations

import json
from pathlib import Path

from _harness import SID, setup_environment

from grokbuild import hook, settlement
from grokbuild.state import default_state_path, load_state
from grokbuild.task_evidence import TaskSpec, attach_task_spec
from grokbuild.transactions import bind_terminal_task_tx, ensure_execution_tx, record_decision_tx

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seed_prior_debt(tmp_path: Path, monkeypatch, task_id: str, *, current_profile: str = "default"):
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


def _spawn_result(task_id: str, output: str) -> dict:
    return {
        "sessionId": SID,
        "workspaceRoot": str(REPO_ROOT),
        "toolName": "spawn_subagent",
        "toolInput": {"subagent_type": "implement-hard"},
        "toolResult": {"task_id": task_id, "status": "completed", "output": output},
    }


def test_prior_strict_debt_remains_strict_after_current_route_flips_to_legacy(tmp_path, monkeypatch):
    task_id = "prior-task-without-evidence"
    path, prior = _seed_prior_debt(tmp_path, monkeypatch, task_id)
    hook.handle_post_tool(_spawn_result(task_id, "Completed successfully."), {})
    track = load_state(path).get_execution(prior)
    assert track is not None
    assert "implement-hard" not in track.completed


def test_prior_strict_debt_accepts_typed_evidence_after_current_route_flips_to_legacy(tmp_path, monkeypatch):
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
