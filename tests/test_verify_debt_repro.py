#!/usr/bin/env python3
"""Regression replay for cross-turn background verifier debt fan-out.

Two same-session debt tracks both owe the same two deterministic verifiers.
Each verifier acknowledgement must bind to both tracks, each retrieval must
resolve the matching step on both tracks, and the Stop hook must emit no
session_debt for either decision.
"""

from __future__ import annotations

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"

import contextlib
import copy
import inspect
import io
import json
import os
import tempfile

FIXTURES = ROOT / "fixtures" / "task-payloads"

from grokbuild import hook as hook_route
import grokbuild.settlement as settlement  # noqa: E402
from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.decision import ExecutionMember, ExecutionStage  # noqa: E402
from grokbuild.payloads import task_ids, terminal_background_ack  # noqa: E402
from grokbuild.state import RuntimeState, default_state_path, load_state, save_state

SID = "11111111-1111-7111-8111-111111111111"
DEBT_ID = "7dc8455b41ed9ede1c14"
COMPETITOR_ID = "f6c153159c0a15fe953a"
DEBT_IDS = (DEBT_ID, COMPETITOR_ID)
COMMANDS = ("./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh")
TASK_IDS = (
    "22222222-2222-7222-8222-222222222222",
    "33333333-3333-7333-8333-333333333333",
)
FAILURES: list[str] = []


def _line(obj: object, needle: str, path: str) -> str:
    lines, start = inspect.getsourcelines(obj)
    for offset, text in enumerate(lines):
        if needle in text:
            return f"{path}:{start + offset}"
    return path


def _ok(message: str) -> None:
    print(f"ok   {message}")


def _fail(message: str) -> None:
    FAILURES.append(message)
    print(f"FAIL {message}")


def _check(condition: bool, message: str) -> None:
    (_ok if condition else _fail)(message)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _plant_session(grok_home: Path) -> None:
    session = grok_home / "sessions" / "ws" / SID
    session.mkdir(parents=True, exist_ok=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": SID}, "current_model_id": "glm-5.3-flash"}),
        encoding="utf-8",
    )
    records = (
        {
            "type": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "<user_query>route=implement: repair the routing state machine and run both verifiers"
                        "</user_query>"
                    ),
                }
            ],
        },
        {
            "type": "assistant",
            "content": "Implementation and review completed; verification is pending.",
        },
        {
            "type": "user",
            "content": [
                {"type": "text", "text": ("<user_query>what is the current status?</user_query>")}
            ],
        },
    )
    (session / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _stages() -> tuple[ExecutionStage, ...]:
    return (
        ExecutionStage(
            "recon",
            True,
            "delegated reconnaissance",
            kind="parallel_spawn",
            stage_id="recon",
            members=(
                ExecutionMember("0", "explore", True, "repository map"),
                ExecutionMember("1", "explore", True, "callers and tests"),
            ),
        ),
        ExecutionStage("implement-standard", True, "implementation"),
        ExecutionStage("review-hard", True, "review"),
        ExecutionStage(
            "verify",
            True,
            "routing verifier",
            kind="verify",
            command=COMMANDS[0],
            stage_id="verify/0",
        ),
        ExecutionStage(
            "verify",
            True,
            "combine installer verifier",
            kind="verify",
            command=COMMANDS[1],
            stage_id="verify/1",
        ),
    )


def _seed_two_debt(current_id: str) -> None:
    state = RuntimeState(source_path=default_state_path())
    state.turns[SID] = 2
    state.prompts[SID] = "what is the current status?"
    for debt_id in DEBT_IDS:
        debt = state.set_execution(debt_id, SID, 1, _stages())
        debt.requested = ["recon/0", "recon/1", "implement-standard", "review-hard"]
        debt.completed = ["recon/0", "recon/1", "implement-standard", "review-hard"]
        debt.verified = []
        debt.verify_tasks = {}
        debt.stop_blocks = 1
    state.set_execution(current_id, SID, 2, ())
    save_state(state)


def _payload(name: str, command: str, task_id: str) -> dict:
    data = copy.deepcopy(_fixture(name))
    data.pop("subagentType", None)
    data["sessionId"] = SID
    data["cwd"] = str(REPO_ROOT)
    data["workspaceRoot"] = str(REPO_ROOT)
    data["toolInput"]["task_ids" if name.startswith("retrieval") else "command"] = (
        [task_id] if name.startswith("retrieval") else command
    )
    result = data["toolResult"]
    inner = result["Result"] if result.get("type") == "TaskOutput" else result
    inner["task_id"] = task_id
    inner["command"] = command
    return data


def _debt_matches(command: str, current_id: str) -> list[tuple[str, str]]:
    state = load_state(default_state_path())
    matches: list[tuple[str, str]] = []
    for decision_id, track in state.executions.items():
        if decision_id == current_id or track.session_id != SID:
            continue
        if track.next_required_barrier_or_role() is not None:
            continue
        step = track.next_verify_step()
        if not step or step in track.verify_tasks.values():
            continue
        expected = next(
            (stage.command for stage in track.stages if (stage.stage_id or stage.role) == step), ""
        )
        if hook_route._is_verify_command(command, expected):
            matches.append((decision_id, step))
    return matches


def _step_ack(spec: dict, command: str, task_id: str, expected_step: str, current_id: str) -> None:
    data = _payload("terminal-ack-verify-background.json", command, task_id)
    ack_line = _line(
        hook_route.handle_post_tool, "terminal_background_ack(data)", "grokbuild/settlement.py"
    )
    ids_line = _line(hook_route.handle_post_tool, "task_ids = task_ids", "grokbuild/settlement.py")
    gate_line = _line(
        hook_route.handle_post_tool, "_gate_active(route, spec)", "grokbuild/settlement.py"
    )
    debt_line = _line(
        hook_route.handle_post_tool, "bind_debt_verify_task_tx(", "grokbuild/settlement.py"
    )
    match_line = _line(
        settlement.bind_debt_verify_task_tx, "if not matches:", ".grok/routing/state.py"
    )

    recognized = terminal_background_ack(data)
    extracted = task_ids(data, include_result=True)
    verifier_commands = hook_route.resolve_verifier(str(REPO_ROOT), spec) or ()
    command_match = any(
        hook_route._is_verify_command(command, expected) for expected in verifier_commands
    )
    current_route = hook_route.resolve_route(
        SID, spec, event="post_tool_use", workspace_root=str(REPO_ROOT)
    )
    gate_active = hook_route._gate_active(current_route, spec)
    matches_before = _debt_matches(command, current_id)
    calls: list[tuple] = []
    real_bind = settlement.bind_debt_verify_task_tx

    def observed_bind(*args, **kwargs):
        calls.append(args)
        return real_bind(*args, **kwargs)

    settlement.bind_debt_verify_task_tx = observed_bind
    try:
        hook_route.handle_post_tool(data, spec)
    finally:
        settlement.bind_debt_verify_task_tx = real_bind

    state = load_state(default_state_path())
    bound = {
        debt_id: state.get_execution(debt_id).verify_tasks.get(task_id) for debt_id in DEBT_IDS
    }
    if all(step == expected_step for step in bound.values()):
        _ok(f"Step A bound {task_id} to every debt track as {expected_step} ({bound})")
        return

    _fail(
        f"Step A did not bind {task_id} as {expected_step} on every debt track for {command} "
        f"(bound={bound!r})"
    )
    _check(recognized, f"{ack_line} acknowledgement recognized (actual={recognized})")
    _check(extracted == [task_id], f"{ids_line} task ids extracted (actual={extracted!r})")
    _check(
        command_match,
        (
            f"{ack_line} configured command match (command={command!r}, configured={verifier_commands!r})"
        ),
    )
    _check(
        not gate_active,
        (
            f"{gate_line} current no-intent gate is inactive so debt fallback is reachable "
            f"(actual={gate_active})"
        ),
    )
    _check(bool(calls), f"{debt_line} debt transaction called (calls={len(calls)})")
    _check(
        len(matches_before) == len(DEBT_IDS),
        (
            f"{match_line} both debt tracks are candidates (matches={matches_before!r}); "
            "zero means a candidate guard rejected them, fewer means one track was missed"
        ),
    )


def _step_retrieval(
    spec: dict, command: str, task_id: str, expected_step: str, current_id: str
) -> None:
    data = _payload("retrieval-verify-completed.json", command, task_id)
    status_line = _line(
        hook_route.handle_post_tool,
        'status in {"incomplete", "not_found"}',
        "grokbuild/settlement.py",
    )
    record_line = _line(
        settlement.record_verify_retrieval_tx, "if not matches:", ".grok/routing/state.py"
    )
    session_line = _line(
        settlement.record_verify_retrieval_tx,
        "track.session_id == session_id",
        ".grok/routing/state.py",
    )
    exclusion_line = _line(
        settlement.record_verify_retrieval_tx,
        "decision_id != current_decision_id",
        ".grok/routing/state.py",
    )

    before = load_state(default_state_path())
    bindings = [
        (decision_id, track.session_id, step)
        for decision_id, track in before.executions.items()
        for bound_id, step in track.verify_tasks.items()
        if bound_id == task_id
    ]
    statuses = settlement._retrieval_task_statuses(data)
    aggregate = settlement._retrieval_result_status(data)
    hook_route.handle_post_tool(data, spec)
    state = load_state(default_state_path())
    verified = {debt_id: state.get_execution(debt_id).verified for debt_id in DEBT_IDS}
    if all(expected_step in steps for steps in verified.values()):
        _ok(f"Step B recorded {expected_step} on every debt track from {task_id}")
        return

    _fail(
        f"Step B did not verify {expected_step} on every debt track from {task_id} "
        f"(verified={verified!r})"
    )
    _check(
        len(bindings) >= len(DEBT_IDS), f"{record_line} bound tracks found (bindings={bindings!r})"
    )
    _check(
        bool(bindings) and all(sid == SID for _d, sid, _s in bindings),
        (f"{session_line} binding session filter (expected={SID}, bindings={bindings!r})"),
    )
    _check(
        all(decision_id != current_id for decision_id, _sid, _step in bindings),
        (
            f"{exclusion_line} debt decisions differ from current decision "
            f"(current={current_id}, bindings={bindings!r})"
        ),
    )
    terminal = statuses.get(task_id, aggregate) == "success"
    _check(
        terminal,
        (
            f"{status_line} retrieval status is terminal success "
            f"(task_statuses={statuses!r}, aggregate={aggregate!r})"
        ),
    )


def _step_stop(spec: dict, current_id: str) -> None:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        hook_route.handle_stop(
            {
                "hookEventName": "Stop",
                "sessionId": SID,
                "reason": "end_turn",
                "stopHookActive": True,
                "cwd": str(REPO_ROOT),
                "workspaceRoot": str(REPO_ROOT),
            },
            spec,
        )
    text = output.getvalue()
    debt = load_state(default_state_path()).latest_session_debt(
        SID, 86400.0, excluded_decision_id=current_id
    )
    if debt is None and '"decision": "block"' not in text:
        _ok("Step C end_turn has no session_debt block")
        return
    line = _line(hook_route.handle_stop, "latest_session_debt(", ".grok/hooks/route.py")
    debt_summary = (
        None if debt is None else (debt[0], debt[1].next_verify_step(), debt[1].verify_tasks)
    )
    _fail(
        f"Step C remains blocked by session debt at {line}: debt={debt_summary!r}, output={text.strip()!r}"
    )


def test_verify_debt_repro() -> int:
    with tempfile.TemporaryDirectory(prefix="verify-debt-repro-") as raw_tmp:
        tmp = Path(raw_tmp)
        os.environ["GROK_HOME"] = str(tmp / "grok")
        os.environ["XDG_STATE_HOME"] = str(tmp / "state")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
        os.environ.pop("GROK_ROUTE_MODE", None)
        os.environ.pop("GROK_ROUTE_ENFORCE", None)
        os.environ.pop("GROK_ROUTE_SOFT", None)
        _plant_session(tmp / "grok")
        spec = hook_route.apply_config_verifiers(load_intents())
        current_route = hook_route.resolve_route(
            SID, spec, event="post_tool_use", workspace_root=str(REPO_ROOT)
        )
        current_id = current_route["decision_id"]
        _seed_two_debt(current_id)
        _check(
            not hook_route._gate_active(current_route, spec),
            (f"setup current turn is no-intent and gate inactive (decision={current_id})"),
        )

        for index, (command, task_id) in enumerate(zip(COMMANDS, TASK_IDS)):
            _step_ack(spec, command, task_id, f"verify/{index}", current_id)
            _step_retrieval(spec, command, task_id, f"verify/{index}", current_id)
        _step_stop(spec, current_id)

    if FAILURES:
        print(f"REPRO: {len(FAILURES)} failing condition(s) reproduce unclosable verify debt")
        for failure in FAILURES:
            print(f"  - {failure}")
        return
    print("PASS: both cross-turn verify steps closed on every debt track; no session_debt remains")
    return


def main() -> int:
    test_verify_debt_repro()


if __name__ == "__main__":
    run_standalone(main)
