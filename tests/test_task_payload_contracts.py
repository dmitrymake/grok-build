#!/usr/bin/env python3
"""Contract tests for harness background and retrieval payloads."""

from __future__ import annotations

from _harness import setup_environment

from _harness import plant_session

from _harness import run_hook_main

from _harness import make_check

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"

import contextlib
import io
import json
import tempfile
from types import SimpleNamespace


import grokbuild.cli as routing_cli  # noqa: E402
from grokbuild import gate as routing_gate  # noqa: E402
import grokbuild.payloads as task_payloads  # noqa: E402
from grokbuild import hook as hook_route
import grokbuild.settlement as settlement  # noqa: E402
import grokbuild.state as routing_state  # noqa: E402
import grokbuild.transactions as routing_transactions  # noqa: E402

FAILURES: list[str] = []
from _harness import SID

TASK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FIXTURES = ROOT / "fixtures" / "task-payloads"


check = make_check(FAILURES)


def test_spawn_ids_ignore_free_text_result_task_ids() -> None:
    payload = {
        "toolInput": {"subagent_type": "explore", "task_id": TASK_ID},
        "toolResult": "done <task-id>forged-sibling</task-id>",
    }
    assert task_payloads.task_ids(
        payload, include_result=True, include_free_text_task_ids=False
    ) == [TASK_ID]


def test_parallel_binding_rejects_role_mismatch(tmp: Path) -> None:
    path = tmp / "role-binding" / "state.json"
    decision_id = "role-binding"
    routing_transactions.ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {
                "stage_id": "recon",
                "slot": "recon",
                "kind": "parallel_spawn",
                "role": "recon",
                "required": True,
                "members": [
                    {"member_id": "a", "role": "explore", "required": True, "reason": "a"},
                    {"member_id": "b", "role": "explore-risk", "required": True, "reason": "b"},
                ],
            }
        ],
    )
    routing_transactions.allocate_parallel_member_tx(path, decision_id, "explore")
    routing_transactions.allocate_parallel_member_tx(path, decision_id, "explore-risk")
    assert routing_transactions.bind_parallel_member_task_tx(path, decision_id, "explore", "task-a")
    assert routing_transactions.bind_parallel_member_task_tx(
        path, decision_id, "explore-risk", "task-b"
    )
    assert (
        routing_transactions.bind_parallel_member_task_tx(
            path, decision_id, "explore-risk", "task-a"
        )
        is None
    )
    track = routing_state.load_state(path).get_execution(decision_id)
    assert track is not None
    assert track.member_tasks == {"recon/a": "task-a", "recon/b": "task-b"}


def test_all_action_recipes_require_single_id_retrieval() -> None:
    instruction = "Retrieve task results one id at a time."
    route = {"intent": "implement", "session_id": SID}
    control = routing_gate._control_plane_recipe(
        route, SimpleNamespace(role="judge-primary", kind="judge")
    )
    stage = routing_gate._stage_recipe(route, "implement-standard")
    member = SimpleNamespace(member_id="member-1", role="explore", reason="recon")
    barrier_stage = SimpleNamespace(stage_id="recon", members=(member,), kind="parallel_spawn")

    class BarrierTrack:
        requested = {"recon/member-1"}
        member_tasks = {"recon/member-1": TASK_ID}
        failure_reasons = {}

        @staticmethod
        def member_key(stage_id: str, member_id: str) -> str:
            return f"{stage_id}/{member_id}"

        @staticmethod
        def incomplete_required_members(_stage):
            return [member]

    barrier = routing_gate._barrier_recipe(route, BarrierTrack(), barrier_stage)

    class DebtTrack:
        stages = ()
        completed = ()
        requested = {"recon/member-1"}
        member_tasks = {"recon/member-1": TASK_ID}

        @staticmethod
        def member_key(stage_id: str, member_id: str) -> str:
            return f"{stage_id}/{member_id}"

        @staticmethod
        def incomplete_required_members(_stage):
            return [member]

        @staticmethod
        def next_verify_step() -> str:
            return ""

    debt_stage = barrier_stage
    original_next_stage = hook_route._next_required_spawn_stage
    hook_route._next_required_spawn_stage = lambda _track: debt_stage
    try:
        debt = hook_route._debt_recipe(DebtTrack(), SID)
    finally:
        hook_route._next_required_spawn_stage = original_next_stage
    assert TASK_ID in debt
    assert f"Retrieve task {TASK_ID} results" in debt

    for name, recipe in {
        "control-plane": control,
        "linear stage": stage,
        "barrier": barrier,
        "Stop debt": debt,
    }.items():
        check(instruction in recipe, f"{name} recipe requires per-id retrieval")


def run_main(payload: dict) -> tuple[int, str]:
    return run_hook_main(payload, workspace_root=REPO_ROOT)


def test_payload_helpers() -> None:
    multiline = (FIXTURES / "background-ack-multiline.txt").read_text()
    singleline = (FIXTURES / "background-ack-singleline.txt").read_text()
    ack_dict = json.loads((FIXTURES / "background-ack-dict.json").read_text())
    check(
        task_payloads.background_ack_id(multiline) == TASK_ID,
        "multiline background ack extracts id",
    )
    check(
        task_payloads.background_ack_id(singleline) == TASK_ID,
        "single-line background ack extracts id",
    )
    check(
        task_payloads.task_ids({"toolResult": ack_dict}, include_result=True) == [TASK_ID],
        "dict background ack extracts id",
    )
    plural_ids = [TASK_ID, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]
    check(
        task_payloads.task_ids({"toolInput": {"task_ids": plural_ids}}) == plural_ids,
        "plural retrieval input extracts every task id",
    )
    terminal = (FIXTURES / "retrieval-terminal.txt").read_text()
    running = (FIXTURES / "retrieval-running.txt").read_text()
    failed = (FIXTURES / "retrieval-failed.txt").read_text()
    cancelled = (FIXTURES / "retrieval-cancelled.txt").read_text()
    nonzero_exit = (FIXTURES / "retrieval-nonzero-exit.txt").read_text()
    retrieval_dict = json.loads((FIXTURES / "retrieval-dict.json").read_text())
    not_found_single = (FIXTURES / "retrieval-not-found-single.txt").read_text()
    not_found_mixed = (FIXTURES / "retrieval-not-found-mixed.txt").read_text()
    foreground = (FIXTURES / "spawn-foreground-success.txt").read_text()
    check(
        settlement._retrieval_result_status({"toolResult": terminal}) == "success",
        "terminal retrieval is success",
    )
    check(
        settlement._retrieval_result_status({"toolResult": running}) == "incomplete",
        "running retrieval is incomplete",
    )
    check(
        settlement._retrieval_result_status({"toolResult": failed}) == "failure",
        "failed retrieval is failure",
    )
    check(
        settlement._retrieval_result_status({"toolResult": cancelled}) == "failure",
        "cancelled retrieval is failure",
    )
    check(
        settlement._retrieval_result_status({"toolResult": nonzero_exit}) == "failure",
        "nonzero retrieval exit is failure",
    )
    check(
        settlement._retrieval_result_status({"toolResult": retrieval_dict}) == "success",
        "dict retrieval is success",
    )
    check(
        settlement._retrieval_task_statuses(
            {"toolResult": not_found_single}, requested_ids=["01a07e54"]
        )
        == {"01a07e54": "not_found"},
        "single-id operator not-found response is typed",
    )
    check(
        settlement._retrieval_task_statuses(
            {"toolResult": not_found_mixed},
            requested_ids=["completed-sibling", "missing-a", "missing-b"],
        )
        == {
            "completed-sibling": "incomplete",
            "missing-a": "not_found",
            "missing-b": "not_found",
        },
        "mixed batch preserves per-id not-found statuses under the text clamp",
    )
    multi = "=== Multi-wait ===\n=== Task a ===\nStatus: completed\nExit Code: 0\n=== Task b ===\nStatus: completed\nExit Code: 0\n"
    check(
        settlement._retrieval_result_status({"toolResult": {"output": multi}}) == "success",
        "text multi-wait batch without id context keeps its legacy aggregate",
    )
    check(
        settlement._retrieval_result_status(
            {"toolResult": {"output": multi}}, requested_ids=["a", "b"]
        )
        == "incomplete",
        "multi-id text-only completion clamps the aggregate to incomplete",
    )
    mixed_running = multi.replace(
        "Status: completed\nExit Code: 0\n=== Task b", "Status: running\n=== Task b"
    )
    check(
        settlement._retrieval_result_status({"toolResult": mixed_running}, requested_ids=["a", "b"])
        == "incomplete",
        "multi-wait running member keeps retrieval incomplete",
    )
    mixed_failed = multi.replace(
        "Status: completed\nExit Code: 0\n=== Task b", "Status: failed\nExit Code: 1\n=== Task b"
    )
    check(
        settlement._retrieval_result_status({"toolResult": mixed_failed}, requested_ids=["a", "b"])
        == "incomplete",
        "multi-wait text-success member clamps the aggregate below failure",
    )
    live = (
        "=== Multi-wait (wait_all) ===\n"
        f"--- Task {plural_ids[0]} [completed] ---\nExit Code: 0\nfirst result\n"
        f"--- Task {plural_ids[1]} [completed] ---\nExit Code: 0\nsecond result\n"
    )
    check(
        settlement._retrieval_result_status({"toolResult": live}) == "success",
        "live wait-all completed envelope keeps its legacy aggregate without id context",
    )
    check(
        settlement._retrieval_task_statuses({"toolResult": live})
        == {
            plural_ids[0]: "success",
            plural_ids[1]: "success",
        },
        "live wait-all sections map statuses by task id without id context",
    )
    check(
        settlement._retrieval_task_statuses({"toolResult": live}, requested_ids=plural_ids)
        == {
            plural_ids[0]: "incomplete",
            plural_ids[1]: "incomplete",
        },
        "multi-id batch clamps text-header success downward to incomplete",
    )
    live_running = live.replace(
        f"{plural_ids[1]} [completed]", f"{plural_ids[1]} [running]"
    ).replace("Exit Code: 0\nsecond result", "second result")
    check(
        settlement._retrieval_result_status({"toolResult": live_running}, requested_ids=plural_ids)
        == "incomplete",
        "live wait-all running member keeps retrieval incomplete",
    )
    live_failed = live.replace(f"{plural_ids[1]} [completed]", f"{plural_ids[1]} [failed]").replace(
        "Exit Code: 0\nsecond result", "Exit Code: 1\nsecond result"
    )
    check(
        settlement._retrieval_result_status({"toolResult": live_failed}, requested_ids=plural_ids)
        == "incomplete",
        "live wait-all text-success member clamps the aggregate below failure",
    )
    blocks = [{"type": "text", "text": "Status: completed\nExit Code: 0"}]
    check(
        settlement._retrieval_result_status({"toolResult": {"content": blocks}}) == "success",
        "content blocks envelope is success",
    )
    check(
        settlement._retrieval_result_status({"toolResult": {"summary": terminal}}) == "success",
        "single-id summary envelope is success",
    )
    check(
        task_payloads.spawn_result_status({"toolResult": foreground}) == "success",
        "foreground success is success",
    )
    check(
        task_payloads.spawn_result_status({"toolResult": multiline}) == "incomplete",
        "background ack is incomplete",
    )


def test_payload_module_compatibility() -> None:
    payload_cases: list[tuple[str, dict]] = []
    for fixture in sorted(FIXTURES.iterdir()):
        if fixture.suffix == ".txt":
            payload_cases.append((fixture.name, {"toolResult": fixture.read_text()}))
        elif fixture.suffix == ".json" and fixture.name != "terminal-background-captured.json":
            value = json.loads(fixture.read_text())
            payload_cases.append(
                (fixture.name, value if "toolResult" in value else {"toolResult": value})
            )
    payload_cases.extend(
        [
            (
                "content blocks",
                {
                    "toolResult": {
                        "content": [{"type": "text", "text": "Status: completed\nExit Code: 0"}]
                    }
                },
            ),
            (
                "structured synthesis",
                {
                    "toolResult": {
                        "type": "TaskOutput",
                        "Result": {
                            "task_id": TASK_ID,
                            "status": "completed",
                            "exit_code": 0,
                            "output": "done",
                        },
                    }
                },
            ),
            (
                "capital Result preserved",
                {
                    "toolResult": {
                        "Result": {"task_id": "other", "status": "completed"},
                        "task_id": TASK_ID,
                        "status": "running",
                        "output": "partial",
                    }
                },
            ),
        ]
    )
    for label, payload in payload_cases:
        check(
            task_payloads.tool_result_raw(payload) == task_payloads.tool_result_raw(payload),
            f"payload module raw-result compatibility: {label}",
        )
        check(
            task_payloads.spawn_result_status(payload)
            == task_payloads.spawn_result_status(payload),
            f"payload module spawn compatibility: {label}",
        )
        check(
            task_payloads.retrieval_result_status(payload)
            == settlement._retrieval_result_status(payload),
            f"payload module retrieval compatibility: {label}",
        )
        check(
            task_payloads.retrieval_task_statuses(payload)
            == settlement._retrieval_task_statuses(payload),
            f"payload module task-status compatibility: {label}",
        )


def test_cli_contract_check(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    retrieval = json.loads((FIXTURES / "retrieval-captured-single.json").read_text())
    terminal = json.loads((FIXTURES / "terminal-background-captured.json").read_text())

    def write_capture(path: Path, payloads: list[dict]) -> None:
        path.write_text(
            "".join(
                json.dumps({"observed_at": f"capture-{index}", "payload": payload}) + "\n"
                for index, payload in enumerate(payloads)
            ),
            encoding="utf-8",
        )

    def invoke(path: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = routing_cli.main(["contract-check", "--capture", str(path)])
        return rc, stdout.getvalue(), stderr.getvalue()

    capture = tmp / "payload-debug.jsonl"
    older_drift = json.loads(json.dumps(retrieval))
    older_drift["toolResult"]["Result"]["exit_code"] = "0"
    write_capture(capture, [older_drift, retrieval, terminal])
    rc, out, err = invoke(capture)
    check(
        rc == 0 and out == "OK: latest payload contracts match\n" and not err,
        "contract-check selects latest good records and prints exact success line",
    )

    conductor_retrieval = json.loads(json.dumps(retrieval))
    conductor_retrieval.pop("subagentType", None)
    conductor_terminal = json.loads(json.dumps(terminal))
    conductor_terminal.pop("subagentType", None)
    write_capture(capture, [retrieval, terminal, conductor_retrieval, conductor_terminal])
    rc, out, err = invoke(capture)
    check(
        rc == 0 and out == "OK: latest payload contracts match\n" and not err,
        "contract-check accepts captures with and without host session markers",
    )

    write_capture(capture, [retrieval])
    rc, out, err = invoke(capture)
    check(
        rc == 0 and out == "OK: latest payload contracts match\n" and not err,
        "contract-check permits a retrieval-only capture",
    )

    type_drift = json.loads(json.dumps(retrieval))
    type_drift["toolResult"]["Result"]["exit_code"] = "0"
    write_capture(capture, [type_drift])
    rc, out, err = invoke(capture)
    check(
        rc == 1
        and not out
        and "get_command_or_subagent_output.toolResult.Result.exit_code: wrong type" in err,
        "contract-check reports a path-qualified scalar type drift",
    )

    key_drift = json.loads(json.dumps(retrieval))
    del key_drift["toolResult"]["Result"]["raw_output_bytes"]
    write_capture(capture, [key_drift])
    rc, out, err = invoke(capture)
    check(
        rc == 1
        and not out
        and "get_command_or_subagent_output.toolResult.Result: missing key 'raw_output_bytes'"
        in err,
        "contract-check reports a path-qualified missing key",
    )

    missing = tmp / "missing.jsonl"
    rc, out, err = invoke(missing)
    check(
        rc == 1
        and not out
        and "Probe: touch ~/.local/state/grok-route/payload-debug.enabled" in err,
        "contract-check missing capture exits one with probe instructions",
    )
    capture.write_text("\ninvalid json\n", encoding="utf-8")
    rc, out, err = invoke(capture)
    check(
        rc == 1
        and not out
        and "no valid records" in err
        and "get_command_or_subagent_output" in err,
        "contract-check blank capture exits one with probe instructions",
    )


def test_contract_check_ignores_foreground_terminal_interleaving(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    retrieval = json.loads((FIXTURES / "retrieval-captured-single.json").read_text())
    background = json.loads((FIXTURES / "terminal-background-captured.json").read_text())
    foreground = json.loads(json.dumps(background))
    foreground["toolInput"]["background"] = False
    foreground["toolResult"] = {"type": "Bash", "output": "foreground complete", "exit_code": 0}
    capture = tmp / "payload-debug.jsonl"
    capture.write_text(
        "".join(
            json.dumps({"observed_at": str(index), "payload": payload}) + "\n"
            for index, payload in enumerate((background, foreground, retrieval))
        ),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = routing_cli.main(["contract-check", "--capture", str(capture)])
    check(
        rc == 0
        and stdout.getvalue() == "OK: latest payload contracts match\n"
        and not stderr.getvalue(),
        "contract-check ignores a later foreground terminal result",
    )


def test_contract_check_reports_background_terminal_drift_without_leaking_values(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    retrieval = json.loads((FIXTURES / "retrieval-captured-single.json").read_text())
    background = json.loads((FIXTURES / "terminal-background-captured.json").read_text())
    drift = json.loads(json.dumps(background))
    secret = "sk-LEAKED-contract-diagnostic"
    drift["toolResult"]["type"] = secret
    capture = tmp / "payload-debug.jsonl"
    capture.write_text(
        "".join(
            json.dumps({"observed_at": str(index), "payload": payload}) + "\n"
            for index, payload in enumerate((background, drift, retrieval))
        ),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = routing_cli.main(["contract-check", "--capture", str(capture)])
    error = stderr.getvalue()
    check(
        rc == 1
        and not stdout.getvalue()
        and "run_terminal_command.toolResult.type: invariant value" in error
        and "got type string" in error
        and secret not in error,
        "contract-check reports latest background drift without printing captured values",
    )


def test_workspace_verifier_scope(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    setup_environment(tmp)
    foreign = tmp / "foreign-workspace"
    foreign.mkdir()
    commands = ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]
    command = commands[0]
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
            "workspaceRoot": str(foreign),
        }
    )
    check(
        rc != 0 and '"decision": "allow"' not in out,
        "dotfiles verifier is denied in a foreign workspace",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": commands[1]},
            "workspaceRoot": str(foreign),
        }
    )
    check(
        rc != 0 and '"decision": "allow"' not in out,
        "combine verifier is denied in a foreign workspace",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
            "workspaceRoot": str(REPO_ROOT),
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, "dotfiles verifier is allowed in its workspace")


def test_retrieval_correlation_ambiguity(tmp: Path) -> None:
    setup_environment(tmp)
    path = routing_state.default_state_path()
    stages = [{"role": "explore", "required": True, "reason": "test", "kind": "spawn"}]
    for decision_id, turn_id in (("current", 2), ("prior", 1)):
        routing_transactions.ensure_execution_tx(path, decision_id, SID, turn_id, stages)
        routing_transactions.record_stage_tx(path, decision_id, "requested", role="explore")
        routing_transactions.bind_terminal_task_tx(path, decision_id, TASK_ID, "explore")
    before = path.read_bytes()
    outcome = routing_transactions.record_retrieval_result_tx(
        path,
        SID,
        "current",
        TASK_ID,
        success=True,
    )
    state = routing_state.load_state(path)
    check(
        outcome == "ambiguous"
        and path.read_bytes() == before
        and all(not track.completed for track in state.executions.values()),
        "same-session task id ambiguity across live tracks changes neither track",
    )


def test_terminal_background_failure_keeps_completed_stage(tmp: Path) -> None:
    setup_environment(tmp)
    path = routing_state.default_state_path()
    decision_id = "terminal-decision"
    stages = [
        {"role": "implement-hard", "required": True, "reason": "test", "kind": "spawn"},
        {"role": "review-hard", "required": True, "reason": "test", "kind": "spawn"},
    ]
    routing_transactions.ensure_execution_tx(path, decision_id, SID, 1, stages)
    for role in ("implement-hard", "review-hard"):
        routing_transactions.record_stage_tx(path, decision_id, "requested", role=role)
        routing_transactions.record_stage_tx(path, decision_id, "result", role=role, success=True)

    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": decision_id,
        "mode": "dynamic",
        "source": "test",
    }
    try:
        background = json.loads((FIXTURES / "terminal-background-captured.json").read_text())
        background.pop("subagentType", None)
        hook_route.handle_post_tool(background, hook_route.load_intents())
        track = routing_state.load_state(path).get_execution(decision_id)
        check(
            track.terminal_tasks == {TASK_ID: "implement-hard"},
            "write-capable background terminal ack binds to the completed implement stage",
        )

        readonly_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        readonly = json.loads(json.dumps(background))
        readonly["toolInput"]["command"] = "ls"
        readonly["toolResult"]["task_id"] = readonly_id
        hook_route.handle_post_tool(readonly, hook_route.load_intents())
        track = routing_state.load_state(path).get_execution(decision_id)
        check(
            readonly_id not in track.terminal_tasks,
            "read-only background terminal ack is not bound",
        )

        failed_text = (FIXTURES / "retrieval-failed.txt").read_text().replace(readonly_id, TASK_ID)
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_id": TASK_ID},
                "toolResult": failed_text,
                "workspaceRoot": str(REPO_ROOT),
            },
            hook_route.load_intents(),
        )
        track = routing_state.load_state(path).get_execution(decision_id)
        check(
            "implement-hard" not in track.failed
            and "implement-hard" in track.completed
            and TASK_ID not in track.terminal_tasks
            and track.all_required_completed(),
            "failed terminal retrieval cannot demote a completed stage",
        )

        routing_transactions.record_stage_tx(
            path, decision_id, "result", role="implement-hard", success=True
        )
        success_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        routing_transactions.bind_terminal_task_tx(path, decision_id, success_id, "implement-hard")
        success_text = (
            (FIXTURES / "retrieval-terminal.txt").read_text().replace(TASK_ID, success_id)
        )
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_id": success_id},
                "toolResult": success_text,
            },
            hook_route.load_intents(),
        )
        track = routing_state.load_state(path).get_execution(decision_id)
        check(
            "implement-hard" in track.completed
            and "implement-hard" not in track.failed
            and success_id not in track.terminal_tasks,
            "successful retrieval keeps a completed stage and clears its terminal binding",
        )
        after_success = track.to_dict()
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_id": success_id},
                "toolResult": (FIXTURES / "retrieval-failed.txt")
                .read_text()
                .replace(TASK_ID, success_id),
            },
            hook_route.load_intents(),
        )
        track = routing_state.load_state(path).get_execution(decision_id)
        check(
            track.to_dict() == after_success,
            "later failed retrieval of a cleaned terminal task is a no-op",
        )

        before = track.to_dict()
        unbound_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        unbound_failure = failed_text.replace(TASK_ID, unbound_id)
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_id": unbound_id},
                "toolResult": unbound_failure,
            },
            hook_route.load_intents(),
        )
        after = routing_state.load_state(path).get_execution(decision_id).to_dict()
        check(after == before, "failed retrieval without a binding does not change execution state")
    finally:
        hook_route.resolve_route = original_route


def test_linear_background_chain(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "route=implement: authentication code"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    # The prompt's reconnaissance is a linear explore stage.
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
            "toolResult": "repository map complete",
        }
    )
    import grokbuild.state as routing_state

    track = next(
        iter(routing_state.load_state(routing_state.default_state_path()).executions.values())
    )
    implementation_role = track.next_required_role()
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": implementation_role, "background": True},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, "linear implementation spawn is allowed")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": implementation_role, "background": True},
            "toolResult": (FIXTURES / "background-ack-multiline.txt").read_text(),
        }
    )
    track = next(
        iter(routing_state.load_state(routing_state.default_state_path()).executions.values())
    )
    check(
        track.stage_tasks.get(implementation_role) == TASK_ID
        and implementation_role not in track.completed,
        "ack binds linear stage without completing it",
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_id": TASK_ID},
            "toolResult": (FIXTURES / "retrieval-terminal.txt").read_text(),
        }
    )
    track = next(
        iter(routing_state.load_state(routing_state.default_state_path()).executions.values())
    )
    check(
        implementation_role in track.completed and implementation_role not in track.stage_tasks,
        "terminal retrieval completes linear stage",
    )
    # Finish the direct review stage so the due workspace verifier is reachable.
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
            "toolResult": "review completed",
        }
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
        }
    )
    check(
        rc == 0
        and '"reason_code": "exact_verifier_allowed"'
        in (tmp / "state" / "grok-route" / "route.jsonl").read_text(),
        "due exact verifier is allowed",
    )


def _observe_retrieval(payload: dict, track: object) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []
    original_record = settlement.record_retrieval_result_tx
    original_route = hook_route.resolve_route

    def record(*args, **kwargs):
        task_id = args[3]
        matches = [
            key
            for key, bound_id in (
                list(track.member_tasks.items()) + list(track.stage_tasks.items())
            )
            if bound_id == task_id
        ]
        if len(set(matches)) == 1:
            kwargs["role"] = matches[0]
            calls.append((args, kwargs))
            return "recorded"
        return "unmatched" if not matches else "ambiguous"

    try:
        settlement.record_retrieval_result_tx = record
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": "decision",
            "mode": "dynamic",
            "source": "test",
        }
        hook_route.handle_post_tool(payload, hook_route.load_intents())
    finally:
        settlement.record_retrieval_result_tx = original_record
        hook_route.resolve_route = original_route
    return calls


def test_real_retrieval_contracts(tmp: Path) -> None:
    setup_environment(tmp)
    captured = json.loads((FIXTURES / "retrieval-captured-single.json").read_text())
    captured.pop("subagentType", None)
    captured_id = captured["toolInput"]["task_ids"][0]
    calls = _observe_retrieval(
        captured,
        SimpleNamespace(
            member_tasks={},
            stage_tasks={"implement-hard": captured_id},
            completed=[],
        ),
    )
    check(
        len(calls) == 1 and calls[0][1].get("success") is True,
        "captured TaskOutput Result envelope completes its bound stage",
    )

    real_single = (FIXTURES / "retrieval-real-single-completed.txt").read_text()
    check(
        settlement._retrieval_result_status({"toolResult": real_single}) == "success",
        "real single completed retrieval text is terminal success",
    )

    batch = (FIXTURES / "retrieval-real-batch-not-found.txt").read_text()
    statuses = settlement._retrieval_task_statuses({"toolResult": batch})
    completed_id = next(task_id for task_id, status in statuses.items() if status == "success")
    missing_ids = [task_id for task_id, status in statuses.items() if status == "not_found"]
    check(
        settlement._retrieval_task_statuses(
            {"toolResult": batch}, requested_ids=[completed_id, *missing_ids]
        )[completed_id]
        == "incomplete",
        "real batch completed member is text-only and clamps to incomplete in multi-id mode",
    )
    calls = _observe_retrieval(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [completed_id, *missing_ids]},
            "toolResult": batch,
            "workspaceRoot": str(REPO_ROOT),
        },
        SimpleNamespace(
            member_tasks={},
            stage_tasks={
                "explore": completed_id,
                **{f"missing-{i}": task_id for i, task_id in enumerate(missing_ids)},
            },
            completed=[],
        ),
    )
    check(
        not calls,
        "real batch records no success: its completed member is text-only "
        "(fixture semantics change is intentional)",
    )
    check(
        settlement._retrieval_result_status({"toolResult": batch}) == "success",
        "not_found members do not poison a terminal batch aggregate without id context",
    )
    check(
        settlement._retrieval_result_status(
            {"toolResult": batch}, requested_ids=[completed_id, *missing_ids]
        )
        == "incomplete",
        "multi-id batch aggregate stays incomplete when every success is text-only",
    )

    running = (FIXTURES / "retrieval-batch-running.txt").read_text()
    running_ids = list(settlement._retrieval_task_statuses({"toolResult": running}))
    calls = _observe_retrieval(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": running_ids},
            "toolResult": running,
        },
        SimpleNamespace(
            member_tasks={},
            stage_tasks={f"running-{i}": task_id for i, task_id in enumerate(running_ids)},
            completed=[],
        ),
    )
    check(not calls, "all-running batch retrieval records no result")

    failed = (FIXTURES / "retrieval-batch-failed.txt").read_text()
    failed_id = next(iter(settlement._retrieval_task_statuses({"toolResult": failed})))
    calls = _observe_retrieval(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [failed_id]},
            "toolResult": failed,
        },
        SimpleNamespace(member_tasks={}, stage_tasks={"explore": failed_id}, completed=[]),
    )
    check(
        len(calls) == 1 and calls[0][1].get("success") is False,
        "failed bracket and nonzero exit record a bound stage failure",
    )

    requested_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    different_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    mismatched = f"=== Task {different_id} ===\nStatus: completed\nExit Code: 0\n"
    calls = _observe_retrieval(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [requested_id]},
            "toolResult": mismatched,
        },
        SimpleNamespace(member_tasks={}, stage_tasks={"explore": requested_id}, completed=[]),
    )
    check(
        not calls, "mismatched single-task result cannot complete or fail the requested bound stage"
    )

    structured_cases = [
        ("missing status", {"task_id": requested_id, "output": "worker still active"}),
        (
            "in_progress status",
            {"task_id": requested_id, "status": "in_progress", "output": "partial"},
        ),
        ("null exit code", {"task_id": requested_id, "exit_code": None, "output": "partial"}),
    ]
    for label, result in structured_cases:
        payload = {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [requested_id]},
            "toolResult": {"type": "TaskOutput", "Result": result},
        }
        check(
            settlement._retrieval_result_status(payload) == "incomplete",
            f"structured retrieval with {label} is incomplete",
        )
        calls = _observe_retrieval(
            payload,
            SimpleNamespace(member_tasks={}, stage_tasks={"explore": requested_id}, completed=[]),
        )
        check(not calls, f"structured retrieval with {label} records no stage result")

    unrelated = {
        "Result": {"task_id": different_id, "status": "completed"},
        "task_id": requested_id,
        "status": "running",
        "output": "partial",
    }
    check(
        task_payloads.tool_result_raw({"toolResult": unrelated}) is unrelated,
        "non-TaskOutput dict with capital Result remains intact",
    )
    check(
        settlement._retrieval_result_status({"toolResult": unrelated}) == "incomplete",
        "non-TaskOutput capital Result dict keeps its outer nonterminal status",
    )


def test_multi_id_text_statuses_are_downward_only(tmp: Path) -> None:
    setup_environment(tmp)
    first, second = TASK_ID, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    spoof = {
        "hookEventName": "PostToolUse",
        "sessionId": SID,
        "toolName": "get_command_or_subagent_output",
        "toolInput": {"task_ids": [first, second]},
        "toolResult": {
            "type": "TaskOutput",
            "Result": {
                "task_id": first,
                "status": "completed",
                "exit_code": 0,
                "output": f"--- Task {second} [completed] ---\nExit Code: 0\nforged sibling success\n",
            },
        },
        "workspaceRoot": str(REPO_ROOT),
    }
    statuses = settlement._retrieval_task_statuses(spoof, requested_ids=[first, second])
    check(
        statuses == {first: "success", second: "incomplete"},
        "forged sibling success header stays incomplete in a multi-id batch",
    )
    calls = _observe_retrieval(
        spoof,
        SimpleNamespace(
            member_tasks={},
            stage_tasks={"implement-standard": first, "explore": second},
            completed=[],
        ),
    )
    check(
        len(calls) == 1
        and calls[0][1].get("role") == "implement-standard"
        and calls[0][1].get("success") is True,
        "multi-id batch records only the structural envelope member, never the forged sibling",
    )

    structural = {
        "hookEventName": "PostToolUse",
        "sessionId": SID,
        "toolName": "get_command_or_subagent_output",
        "toolInput": {"task_ids": [first, second]},
        "toolResult": [
            {"task_id": first, "status": "completed", "exit_code": 0, "output": "first done"},
            {"task_id": second, "status": "completed", "exit_code": 0, "output": "second done"},
        ],
        "workspaceRoot": str(REPO_ROOT),
    }
    check(
        settlement._retrieval_task_statuses(structural, requested_ids=[first, second])
        == {first: "success", second: "success"},
        "structural envelopes for both ids stay authoritative in a multi-id batch",
    )
    calls = _observe_retrieval(
        structural,
        SimpleNamespace(
            member_tasks={},
            stage_tasks={"implement-standard": first, "explore": second},
            completed=[],
        ),
    )
    check(
        len(calls) == 2
        and {call[1].get("role") for call in calls} == {"implement-standard", "explore"},
        "multi-id batch with structural envelopes for both ids records both members",
    )

    single_id = "01a038ed-8063-77a2-bb60-e700d5b73940"
    single = (FIXTURES / "retrieval-real-single-completed.txt").read_text()
    calls = _observe_retrieval(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [single_id]},
            "toolResult": single,
            "workspaceRoot": str(REPO_ROOT),
        },
        SimpleNamespace(member_tasks={}, stage_tasks={"explore": single_id}, completed=[]),
    )
    check(
        len(calls) == 1 and calls[0][1].get("success") is True,
        "single-id text success still records at route level",
    )


def _plant_fallback_child(tmp: Path, task_id: str, *, completed: bool) -> None:
    plant_session(tmp / "grok", "child")
    session = tmp / "grok" / "sessions" / "ws" / task_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": task_id}, **({"status": "completed"} if completed else {})}),
        encoding="utf-8",
    )
    (session / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "work"})
        + "\n"
        + json.dumps({"type": "assistant", "content": "Completed the work successfully."})
        + "\n",
        encoding="utf-8",
    )


def test_not_found_fallback_without_terminal_evidence_does_not_settle_success(tmp: Path) -> None:
    setup_environment(tmp)
    path = routing_state.default_state_path()
    decision_id = "fallback-no-terminal"
    task_id = "01a038ed-8063-77a2-bb60-e700d5b73990"
    routing_transactions.ensure_execution_tx(
        path, decision_id, SID, 1, [{"role": "explore", "required": True, "kind": "spawn"}]
    )
    routing_transactions.bind_terminal_task_tx(path, decision_id, task_id, "explore")
    _plant_fallback_child(tmp, task_id, completed=False)
    original_route = hook_route.resolve_route
    original_statuses = settlement._retrieval_task_statuses
    original_result_status = settlement._retrieval_result_status
    try:
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": decision_id,
            "mode": "dynamic",
        }
        settlement._retrieval_task_statuses = lambda data, requested_ids=None: {
            requested_ids[0]: "not_found"
        }
        settlement._retrieval_result_status = lambda data, requested_ids=None: "not_found"
        for _ in range(2):
            run_main(
                {
                    "hookEventName": "PostToolUse",
                    "sessionId": SID,
                    "toolName": "get_command_or_subagent_output",
                    "toolInput": {"task_ids": [task_id]},
                    "toolResult": {},
                }
            )
    finally:
        hook_route.resolve_route = original_route
        settlement._retrieval_task_statuses = original_statuses
        settlement._retrieval_result_status = original_result_status
    track = routing_state.load_state(path).get_execution(decision_id)
    assert track is not None and "explore" not in track.completed and "explore" in track.failed


def test_not_found_fallback_strict_profile_requires_typed_evidence(tmp: Path) -> None:
    setup_environment(tmp)
    path = routing_state.default_state_path()
    decision_id = "fallback-strict-evidence"
    task_id = "01a038ed-8063-77a2-bb60-e700d5b73991"
    routing_transactions.ensure_execution_tx(
        path, decision_id, SID, 1, [{"role": "explore", "required": True, "kind": "spawn"}]
    )
    routing_transactions.bind_terminal_task_tx(path, decision_id, task_id, "explore")
    _plant_fallback_child(tmp, task_id, completed=True)
    original_route = hook_route.resolve_route
    original_statuses = settlement._retrieval_task_statuses
    original_result_status = settlement._retrieval_result_status
    try:
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": decision_id,
            "mode": "dynamic",
            "profile": "evidence",
        }
        settlement._retrieval_task_statuses = lambda data, requested_ids=None: {
            requested_ids[0]: "not_found"
        }
        settlement._retrieval_result_status = lambda data, requested_ids=None: "not_found"
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_ids": [task_id]},
                "toolResult": {},
            }
        )
    finally:
        hook_route.resolve_route = original_route
        settlement._retrieval_task_statuses = original_statuses
        settlement._retrieval_result_status = original_result_status
    track = routing_state.load_state(path).get_execution(decision_id)
    assert track is not None and "explore" not in track.completed


def test_retrieval_transcript_fallback(tmp: Path) -> None:
    setup_environment(tmp)
    plant_session(tmp / "grok", "route=implement: fallback contract")
    tool_use_id = "call_transcript_fallback"
    history = tmp / "grok" / "sessions" / "ws" / SID / "chat_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_call_id": tool_use_id,
                    "content": [
                        {
                            "type": "text",
                            "text": (FIXTURES / "retrieval-real-single-completed.txt").read_text(),
                        }
                    ],
                }
            )
            + "\n"
        )
    task_id = "01a038ed-8063-77a2-bb60-e700d5b73940"
    payload = {
        "hookEventName": "PostToolUse",
        "sessionId": SID,
        "toolName": "get_command_or_subagent_output",
        "toolUseId": tool_use_id,
        "toolInput": {"task_ids": [task_id]},
        "toolResult": {"type": "TaskOutput", "Result": {}},
    }

    def transcript(data):
        return task_payloads.retrieval_transcript_text(
            data,
            hook_route._session_dir,
            hook_route._session_id,
            hook_route._iter_jsonl_reversed,
        )

    check(
        task_payloads.retrieval_result_status(payload, transcript)
        == settlement._retrieval_result_status(payload),
        "payload module transcript fallback matches route compatibility wrapper",
    )
    calls = _observe_retrieval(
        payload,
        SimpleNamespace(member_tasks={}, stage_tasks={"explore": task_id}, completed=[]),
    )
    check(
        len(calls) == 1 and calls[0][1].get("success") is True,
        "empty direct payload falls back to the matching transcript tool result",
    )


def test_disk_fallback_closes_evicted_binding(tmp: Path) -> None:
    setup_environment(tmp)
    path = routing_state.default_state_path()
    decision_id = "disk-fallback-decision"
    members = [
        {"member_id": "0", "role": "explore", "required": True, "reason": "test"},
        {"member_id": "1", "role": "explore", "required": True, "reason": "test"},
        {"member_id": "2", "role": "explore", "required": True, "reason": "test"},
    ]
    routing_transactions.ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {
                "stage_id": "recon",
                "role": "recon",
                "required": True,
                "kind": "parallel_spawn",
                "members": members,
            }
        ],
    )
    task_ids = [
        "01a038ed-8063-77a2-bb60-e700d5b73901",
        "01a038ed-8063-77a2-bb60-e700d5b73902",
        "01a038ed-8063-77a2-bb60-e700d5b73903",
    ]
    keys = []
    for task_id in task_ids:
        key = routing_transactions.allocate_parallel_member_tx(path, decision_id, "explore")
        keys.append(key)
        routing_transactions.bind_parallel_member_task_tx(path, decision_id, "explore", task_id)
    plant_session(tmp / "grok", "route=implement: disk fallback")
    for task_id, text in (
        (task_ids[0], "Completed the exploration successfully."),
        (task_ids[1], "BLOCKED: pytest failed"),
    ):
        plant_session(tmp / "grok", "child")
        session = tmp / "grok" / "sessions" / "ws" / task_id
        session.mkdir(parents=True, exist_ok=True)
        (session / "summary.json").write_text(
            json.dumps({"info": {"id": task_id}, "status": "completed"}), encoding="utf-8"
        )
        (session / "chat_history.jsonl").write_text(
            json.dumps({"type": "user", "content": "work"})
            + "\n"
            + json.dumps({"type": "assistant", "content": text})
            + "\n",
            encoding="utf-8",
        )

    original_route = hook_route.resolve_route
    original_statuses = settlement._retrieval_task_statuses
    original_result_status = settlement._retrieval_result_status
    try:
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": decision_id,
            "mode": "dynamic",
        }
        settlement._retrieval_task_statuses = lambda data, requested_ids=None: {
            requested_ids[0]: "not_found"
        }
        settlement._retrieval_result_status = lambda data, requested_ids=None: "not_found"
        for task_id in task_ids[:2]:
            run_main(
                {
                    "hookEventName": "PostToolUse",
                    "sessionId": SID,
                    "toolName": "get_command_or_subagent_output",
                    "toolInput": {"task_ids": [task_id]},
                    "toolResult": {},
                }
            )
    finally:
        hook_route.resolve_route = original_route
        settlement._retrieval_task_statuses = original_statuses
        settlement._retrieval_result_status = original_result_status
    track = routing_state.load_state(path).get_execution(decision_id)
    check(
        track is not None and keys[0] in track.completed and keys[0] not in track.not_found_streaks,
        "disk fallback closes a missing binding with success",
    )
    check(
        track is not None and keys[1] in track.failed and keys[1] not in track.completed,
        "BLOCKED disk fallback records failure",
    )

    # Aggregate-only not_found has no authoritative per-task status.
    original_route = hook_route.resolve_route
    original_statuses = settlement._retrieval_task_statuses
    original_result_status = settlement._retrieval_result_status
    try:
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": decision_id,
            "mode": "dynamic",
        }
        settlement._retrieval_task_statuses = lambda data, requested_ids=None: {}
        settlement._retrieval_result_status = lambda data, requested_ids=None: "not_found"
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_ids": [task_ids[2]]},
                "toolResult": {},
            }
        )
    finally:
        hook_route.resolve_route = original_route
        settlement._retrieval_task_statuses = original_statuses
        settlement._retrieval_result_status = original_result_status
    track = routing_state.load_state(path).get_execution(decision_id)
    check(
        track is not None and keys[2] not in track.not_found_streaks,
        "aggregate-only not_found does not mutate a binding streak",
    )


def test_cli_complete(tmp: Path) -> None:
    path = tmp / "manual-state.json"

    def invoke(decision_id: str, role: str) -> tuple[int, str, str]:
        original_default = routing_cli.default_state_path
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            routing_cli.default_state_path = lambda: path
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = routing_cli.cmd_complete(
                    SimpleNamespace(
                        decision_id=decision_id,
                        role=role,
                        failure=False,
                    )
                )
        finally:
            routing_cli.default_state_path = original_default
        return rc, stdout.getvalue(), stderr.getvalue()

    rc, _out, err = invoke("missing-decision", "implement-hard")
    check(
        rc == 2 and "unknown decision_id" in err and not path.exists(),
        "CLI rejects an unknown decision without creating state",
    )

    decision_id = "manual-decision"
    role = "implement-hard"
    routing_transactions.ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {
                "role": role,
                "required": True,
                "reason": "test",
                "kind": "spawn",
            }
        ],
    )
    routing_transactions.record_stage_tx(path, decision_id, "requested", role=role)
    before = path.read_bytes()
    rc, _out, err = invoke(decision_id, "implement-hrad")
    check(
        rc == 2
        and "valid roles/member keys: implement-hard" in err
        and path.read_bytes() == before,
        "CLI rejects an unknown role without changing known execution state",
    )

    rc, out, err = invoke(decision_id, role)
    payload = json.loads(out) if out else {}
    check(
        rc == 0 and not err and payload.get("completed") == [role] and payload.get("failed") == [],
        "CLI complete updates a valid stage on a temporary state path",
    )

    parallel_id = "parallel-decision"
    member_key = "recon/member-0"
    routing_transactions.ensure_execution_tx(
        path,
        parallel_id,
        SID,
        2,
        [
            {
                "role": "recon",
                "required": True,
                "reason": "test",
                "kind": "parallel_spawn",
                "stage_id": "recon",
                "members": [
                    {
                        "member_id": "member-0",
                        "role": "explore",
                        "required": True,
                        "reason": "test",
                    }
                ],
            }
        ],
    )
    routing_transactions.record_stage_tx(path, parallel_id, "requested", role=member_key)
    track = routing_state.load_state(path).get_execution(parallel_id)
    check(member_key not in track.member_tasks, "parallel CLI fixture has no bound task id")
    before = path.read_bytes()
    rc, _out, err = invoke(parallel_id, "recon/unknown")
    check(
        rc == 2 and member_key in err and path.read_bytes() == before,
        "CLI rejects an unknown parallel member key without changing state",
    )

    orphan_key = "recon/orphaned"
    state = routing_state.load_state(path)
    state.get_execution(parallel_id).member_tasks[orphan_key] = "stale-task-id"
    routing_state.save_state(state, path)
    before = path.read_bytes()
    rc, _out, err = invoke(parallel_id, orphan_key)
    check(
        rc == 2 and member_key in err and path.read_bytes() == before,
        "CLI rejects an orphaned bound member key without changing state",
    )

    rc, out, err = invoke(parallel_id, member_key)
    payload = json.loads(out) if out else {}
    check(
        rc == 0 and not err and member_key in payload.get("completed", []),
        "CLI complete accepts a configured parallel member without a task binding",
    )


def test_payload_debug_is_structure_only(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "payload-debug.enabled").touch()
    prompt = "do not retain this prompt"
    task_payloads.dump_payload_debug(
        {"prompt": prompt, "toolResult": {"type": "TaskOutput", "output": "secret result"}},
        tmp,
        "now",
    )
    capture = tmp / "payload-debug.jsonl"
    text = capture.read_text(encoding="utf-8")
    check(
        prompt not in text and "secret result" not in text and '"type": "object"' in text,
        "payload debug capture contains structure only",
    )
    check((capture.stat().st_mode & 0o777) == 0o600, "payload debug capture is mode 0600")


def test_duplicate_text_header_failure_wins() -> None:
    text = (
        "--- Task task-a [failed] ---\nStatus: failed\n=== Output ===\n"
        "=== Task task-a ===\nStatus: completed\nExit Code: 0"
    )
    check(
        task_payloads.retrieval_task_statuses({"toolResult": text}, requested_ids=["task-a"])
        == {"task-a": "failure"},
        "duplicate task headers retain the most-severe status",
    )


def main() -> int:
    test_payload_helpers()
    test_duplicate_text_header_failure_wins()
    test_spawn_ids_ignore_free_text_result_task_ids()
    test_all_action_recipes_require_single_id_retrieval()
    with tempfile.TemporaryDirectory() as directory:
        test_payload_debug_is_structure_only(Path(directory) / "payload-debug")
    test_payload_module_compatibility()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        test_cli_contract_check(root / "contract-check")
        test_contract_check_ignores_foreground_terminal_interleaving(
            root / "foreground-interleaving"
        )
        test_contract_check_reports_background_terminal_drift_without_leaking_values(
            root / "background-drift"
        )
        test_workspace_verifier_scope(root / "workspace")
        test_retrieval_correlation_ambiguity(root / "retrieval-ambiguity")
        test_terminal_background_failure_keeps_completed_stage(root / "terminal-reopen")
        test_linear_background_chain(root / "linear")
        test_real_retrieval_contracts(root / "real")
        test_multi_id_text_statuses_are_downward_only(root / "multi-id-downward")
        test_disk_fallback_closes_evicted_binding(root / "disk-fallback")
        test_not_found_fallback_without_terminal_evidence_does_not_settle_success(
            root / "fallback-no-terminal"
        )
        test_not_found_fallback_strict_profile_requires_typed_evidence(
            root / "fallback-strict-evidence"
        )
        test_retrieval_transcript_fallback(root / "fallback")
        test_cli_complete(root / "cli")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    run_standalone(main)


PYTEST_ONLY = ("test_parallel_binding_rejects_role_mismatch",)
