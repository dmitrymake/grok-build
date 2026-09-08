#!/usr/bin/env python3
"""State-machine acceptance tests for the Grok Build combine hook.

Covers ordered multi-stage spawn enforcement, wrong-stage denial, failed/
unverifiable spawn handling, static cache no-I/O, stale neutral runtime signals,
executor-stage validation/fallback, concurrent state updates, and risk-term
extraction (authentication/authorization).
"""

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

import json
from dataclasses import replace
import multiprocessing as mp
import os
import tempfile
import time


from grokbuild.cache import cache_put  # noqa: E402
from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.decision import ExecutionMember, ExecutionStage  # noqa: E402
from grokbuild.roles import Role, RoleRegistry  # noqa: E402
import grokbuild.roles as routing_roles  # noqa: E402
from grokbuild.router import route_prompt  # noqa: E402
from grokbuild.payloads import task_ids, terminal_background_ack  # noqa: E402
from grokbuild.compose import compose_execution  # noqa: E402
from grokbuild.policy import load_profiles  # noqa: E402
from grokbuild.state import (
    EXECUTION_TTL,
    ExecutionTrack,
    RuntimeState,
    default_log_path,
    default_state_path,
    load_state,
    save_state,
    _canonical_stage_slot,
    _stage_slot,
)
from grokbuild.transactions import (
    allocate_parallel_member_tx,
    bind_parallel_member_task_tx,
    bind_linear_stage_task_tx,
    bind_debt_stage_task_tx,
    bind_debt_verify_task_tx,
    bind_terminal_task_tx,
    bind_verify_task_tx,
    ensure_execution_tx,
    record_decision_tx,
    record_retrieval_result_tx,
    release_unresolvable_binding_tx,
    record_stage_tx,
    record_stop_block_tx,
    record_verify_retrieval_tx,
    record_verify_tx,
    record_verify_fanout_tx,
)
from grokbuild.persist import atomic_update_json
from grokbuild import hook as hook_route
import grokbuild.settlement as settlement  # noqa: E402
from test_state_golden import test_state_v8_golden as run_state_v8_golden  # noqa: E402

FAILURES: list[str] = []
from _harness import SID
from grokbuild.verifiers import _argv_has_shell_metachar, is_verifier_command


check = make_check(FAILURES)


def test_verifier_metachar_controls_rejected() -> None:
    command = "runner 'line\nvalue'"
    check(
        not is_verifier_command(command, command),
        "state verifier rejects a real newline in an argv token",
    )
    check(
        not _argv_has_shell_metachar((r"line\nvalue",)),
        "state verifier allows a literal backslash-n pair",
    )


def test_confirmation_batch_uses_generic_linear_stage_tracking(tmp: Path) -> None:
    path = tmp / "confirmation-state.json"
    composed = compose_execution(
        "implement",
        "low",
        "low",
        "implement-cheap",
        "implement-cheap",
        verify_commands=("verify-one",),
        profile=load_profiles()["confirmation"],
    )
    confirmation = next(stage for stage in composed if stage.kind == "confirmation_batch")
    ensure_execution_tx(path, "confirmation", SID, 1, [confirmation.to_dict()])
    initial = load_state(path).get_execution("confirmation")
    check(
        initial.next_required_role() == "verifier-planner",
        "the composed confirmation stage enters the generic linear queue",
    )
    record_stage_tx(path, "confirmation", "requested", role="verifier-planner")
    bound = bind_linear_stage_task_tx(path, "confirmation", "verifier-planner", "confirmation-task")
    check(bound == "verifier-planner", "the generic linear binder accepts confirmation")
    record_stage_tx(
        path,
        "confirmation",
        "result",
        role="verifier-planner",
        success=True,
        retrieval_task_id="confirmation-task",
    )
    completed = load_state(path).get_execution("confirmation")
    check(
        completed.completed == ["verifier-planner"] and completed.all_required_completed(),
        "generic completion tracking closes the confirmation stage",
    )


def run_main(payload: dict) -> tuple[int, str]:
    return run_hook_main(payload, workspace_root=REPO_ROOT)


def plant_child_session(grok_home: Path, task_id: str, records: list[dict]) -> None:
    session = grok_home / "sessions" / "ws" / task_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": task_id}, "session_kind": "subagent"}),
        encoding="utf-8",
    )
    (session / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def seed_review_verified() -> None:
    """Mark the grok-4.6 review role verified available with an explicit signal."""
    state = RuntimeState(source_path=default_state_path())
    state.set_available("review-independent", True, "grok login verified (test)")
    save_state(state)


def complete_required_explore() -> None:
    track = next(iter(load_state(default_state_path()).executions.values()))
    barrier = next(stage for stage in track.stages if stage.stage_id == "recon")
    for index, member in enumerate(barrier.members):
        run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": member.role},
            }
        )
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": member.role},
                "toolResult": f"recon member {index} complete",
            }
        )


def complete_linear_explore() -> None:
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {
                "tool_name": "spawn_subagent",
                "tool_input": {"subagent_type": "explore"},
            },
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, "nested explore spawn passes its stage gate")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
            "toolResult": "repository map complete",
        }
    )


def test_ordered_multi_stage(tmp: Path) -> None:
    setup_environment(tmp)
    seed_review_verified()
    plant_session(tmp / "grok", "route=implement: authentication code")

    rc, out = run_main(
        {
            "hookEventName": "UserPromptSubmit",
            "sessionId": SID,
            "prompt": "route=implement: authentication code",
        }
    )
    check(rc == 0 and not out.strip(), "submit passive")
    complete_linear_explore()

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard", "prompt": "do it"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"spawn implement-hard allowed ({rc} {out!r})")

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(rc == 2 and '"decision": "deny"' in out, f"write denied before review ({rc} {out!r})")

    rc, out = run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
            "toolResult": "implemented the patch",
        }
    )
    check(rc == 0, "post implement success observed")

    for review_role in ("review-hard", "review-independent"):
        rc, out = run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": review_role, "prompt": "review it"},
            }
        )
        check(rc == 0 and '"decision": "allow"' in out, f"spawn {review_role} allowed ({rc} {out!r})")
        rc, out = run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": review_role},
                "toolResult": "Exit Code: 0\nadversarial review passed",
            }
        )
        check(rc == 0, f"post {review_role} success observed")
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    check(
        rc == 0 and '"decision": "allow"' in out,
        f"deterministic verification command allowed ({rc} {out!r})",
    )

    rc, out = run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )
    check(rc == 0, "post verify success observed")

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        f"conductor write remains denied after all required complete ({rc} {out!r})",
    )

    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": False}
    )
    check(rc == 0 and '"decision": "block"' not in out, "stop not blocked when all stages done")

    state = load_state(tmp / "state" / "grok-route" / "state.json")
    track = next(iter(state.executions.values()), None)
    check(
        track is not None
        and "implement-hard" in track.completed
        and {"review-panel/0", "review-panel/1"}.issubset(track.completed),
        "track records linear and family-independent review completion",
    )
    check(
        track is not None and {"verify/0", "verify/1"}.issubset(track.verified),
        "track records deterministic verification separately",
    )


def test_conductor_recon_diet(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "напиши патч для demo-api"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "monitor",
            "toolInput": {"command": "pwd"},
        }
    )
    records = [
        json.loads(line)
        for line in (tmp / "state" / "grok-route" / "route.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    monitor_outcome = next(
        record
        for record in reversed(records)
        if record.get("event") == "pre_tool_use_outcome" and record.get("tool") == "monitor"
    )
    check(
        rc == 2 and monitor_outcome.get("reason_code") == "recon_diet",
        f"monitor is denied by recon diet while recon is owed ({rc} {out!r})",
    )

    for tool in ("grep", "list_dir", "search", "web_search"):
        rc, out = run_main(
            {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": tool, "toolInput": {}}
        )
        reason = json.loads(out).get("reason", "") if out else ""
        check(
            rc == 2
            and reason.startswith("Stage Gating:")
            and 'route=implement: NEXT: spawn subagent_type="explore"' in reason
            and reason.endswith("[[GROK_ROUTE_STOP_FEEDBACK:v1]]."),
            f"conductor {tool} denied with NEXT-first recon recipe ({rc} {out!r})",
        )
        records = [
            json.loads(line)
            for line in (tmp / "state" / "grok-route" / "route.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        outcome = next(
            record
            for record in reversed(records)
            if record.get("event") == "pre_tool_use_outcome" and record.get("tool") == tool
        )
        check(
            outcome.get("outcome") == "deny" and outcome.get("reason_code") == "recon_diet",
            f"conductor {tool} denial telemetry records recon_diet",
        )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "read_file",
            "toolInput": {"target_file": "README.md"},
        }
    )
    check(
        rc == 0 and '"decision": "allow"' in out,
        "conductor read_file remains allowed for a tight child prompt",
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "rg TODO ."},
        }
    )
    check(rc == 2 and "explore" in out, "read-only shell rg is recon-gated")

    summary = tmp / "grok" / "sessions" / "ws" / SID / "summary.json"
    saved = json.loads(summary.read_text(encoding="utf-8"))
    child_sid = "01a0063b-a2dd-7bf2-aafd-4a66cf837701"
    child_summary = tmp / "grok" / "sessions" / "ws" / child_sid
    child_summary.mkdir(parents=True, exist_ok=True)
    child_summary.joinpath("summary.json").write_text(
        json.dumps({**saved, "info": {"id": child_sid}, "session_kind": "subagent"}), encoding="utf-8"
    )
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": child_sid, "toolName": "grep", "toolInput": {}}
    )
    check(
        rc == 0 and '"decision": "allow"' in out, "subagent grep is exempt from the conductor diet"
    )
    summary.write_text(json.dumps(saved), encoding="utf-8")

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
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "grep", "toolInput": {}}
    )
    check(
        rc == 0 and '"decision": "allow"' in out,
        "conductor grep allowed after required recon completes",
    )


def test_conductor_recon_diet_security_control(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "найди RCE в demo-api"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "grep", "toolInput": {}}
    )
    check(
        rc == 0 and '"decision": "allow"' in out,
        "security intent without pending recon allows conductor grep",
    )


def test_wrong_stage_denied(tmp: Path) -> None:
    setup_environment(tmp)
    plant_session(tmp / "grok", "route=implement: authentication code")
    run_main(
        {
            "hookEventName": "UserPromptSubmit",
            "sessionId": SID,
            "prompt": "route=implement: authentication code",
        }
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "prompt": "skip ahead"},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "explore" in out,
        f"wrong stage denied with required recon recipe ({rc} {out!r})",
    )


def test_failed_spawn_not_success(tmp: Path) -> None:
    setup_environment(tmp)
    plant_session(tmp / "grok", "route=implement: authentication code")
    run_main(
        {
            "hookEventName": "UserPromptSubmit",
            "sessionId": SID,
            "prompt": "route=implement: authentication code",
        }
    )
    complete_linear_explore()
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUseFailure",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
        }
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out, f"failed stage still gates writes ({rc} {out!r})"
    )
    # A failed hard tier is replaced by the next writable implementation tier.
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
        }
    )
    check(
        rc == 2 and "implement-strong" in out,
        f"failed stage falls back before review ({rc} {out!r})",
    )

    # Explicit failure text in a PostToolUse also never succeeds.
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
            "toolResult": "Error: implementation failed",
        }
    )
    state = load_state(tmp / "state" / "grok-route" / "state.json")
    track = next(iter(state.executions.values()), None)
    check(
        track is not None and "implement-hard" not in track.completed,
        "explicit failure not completed",
    )


def test_failed_retrieval_reopens_without_stop_reset(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "explore the codebase read-only, no changes"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
        }
    )
    task_id = "abcdef12"
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore", "background": True},
            "toolResult": f"subagent_id: {task_id}",
        }
    )
    state = load_state(default_state_path())
    decision_id, track = next(iter(state.executions.items()))
    check(
        track.stage_tasks.get("explore") == task_id,
        "background linear stage binds retrieval task id",
    )
    bind_terminal_task_tx(default_state_path(), decision_id, task_id, "explore")
    for _ in range(3):
        record_stop_block_tx(default_state_path(), decision_id)

    run_main(
        {
            "hookEventName": "PostToolUseFailure",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_ids": [task_id]},
        }
    )
    reopened = load_state(default_state_path()).get_execution(decision_id)
    check(
        reopened is not None
        and reopened.failed == ["explore"]
        and "explore" not in reopened.completed,
        "failed retrieval reopens its uniquely bound stage",
    )
    check(
        reopened is not None
        and task_id not in reopened.terminal_tasks
        and "explore" not in reopened.stage_tasks,
        "failed retrieval drops terminal and stage task bindings",
    )
    check(
        reopened is not None and reopened.stop_blocks == 3,
        "failed retrieval does not renew the Stop block budget",
    )


def test_static_cache_no_io(tmp: Path) -> None:
    setup_environment(tmp)
    os.environ["GROK_ROUTE_MODE"] = "static"
    try:
        plant_session(tmp / "grok", "найди RCE в demo-api")
        rc, out = run_main(
            {
                "hookEventName": "UserPromptSubmit",
                "sessionId": SID,
                "prompt": "найди RCE в demo-api",
            }
        )
        check(rc == 0, "static submit ok")
        cache = tmp / "cache" / "grok" / "route.json"
        state = tmp / "state" / "grok-route" / "state.json"
        check(not cache.exists(), "static never writes cache")
        check(not state.exists(), "static never writes state")
    finally:
        os.environ.pop("GROK_ROUTE_MODE", None)


def test_empty_submit_ignores_cached_route(tmp: Path) -> None:
    setup_environment(tmp)
    cache_put(
        {
            "session_id": SID,
            "turn_id": 1,
            "prompt_key": "cached-prompt",
            "decision_id": "cached-decision",
            "intent": "implement",
            "has_strong": True,
        }
    )

    rc, out = run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": ""})
    check(rc == 0 and not out.strip(), "empty submit remains passive")
    log = tmp / "state" / "grok-route" / "route.jsonl"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    check(len(records) == 1, "empty submit persists exactly one record")
    record = records[0]
    check(
        record.get("event") == "user_prompt_submit"
        and record.get("reason") == "no_prompt"
        and isinstance(record.get("decision_id"), str)
        and bool(record["decision_id"]),
        "empty submit persists a no_prompt decision with a valid decision_id",
    )


def test_routing_config_unavailable_records(tmp: Path) -> None:
    original = hook_route.load_intents
    broken = tmp / "broken-config" / "intents.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{broken", encoding="utf-8")
    hook_route.load_intents = lambda: load_intents(broken)
    try:
        pre = tmp / "pre"
        setup_environment(pre)
        rc, out = run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": SID,
                "toolName": "read_file",
                "toolInput": {},
            }
        )
        check(rc == 0 and '"decision": "allow"' in out, "broken routing config allows PreToolUse")
        pre_records = [
            json.loads(line)
            for line in (pre / "state" / "grok-route" / "route.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        check(
            len(pre_records) == 1
            and pre_records[0].get("version") == 4
            and pre_records[0].get("telemetry_generation") == 1
            and pre_records[0].get("event") == "pre_tool_use_outcome"
            and pre_records[0].get("outcome") == "allow"
            and pre_records[0].get("reason_code") == "routing_config_unavailable"
            and bool(pre_records[0].get("decision_id")),
            "broken routing config emits exactly one terminal PreToolUse record",
        )

        for tool_name in ("write", "search_replace", "spawn_subagent"):
            denied = tmp / f"deny-{tool_name}"
            setup_environment(denied)
            rc, out = run_main(
                {
                    "hookEventName": "PreToolUse",
                    "sessionId": SID,
                    "toolName": tool_name,
                    "toolInput": {},
                }
            )
            denied_records = [
                json.loads(line)
                for line in (denied / "state" / "grok-route" / "route.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            check(
                rc == 2
                and '"decision": "deny"' in out
                and len(denied_records) == 1
                and denied_records[0].get("outcome") == "deny"
                and denied_records[0].get("reason_code") == "routing_config_unavailable",
                f"broken routing config denies {tool_name} and records the reason",
            )

        stop = tmp / "stop"
        setup_environment(stop)
        rc, out = run_main(
            {
                "hookEventName": "Stop",
                "sessionId": SID,
                "reason": "end_turn",
                "stopHookActive": False,
            }
        )
        check(rc == 0 and not out.strip(), "broken routing config leaves Stop unblocked")
        stop_records = [
            json.loads(line)
            for line in (stop / "state" / "grok-route" / "route.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        check(
            len(stop_records) == 1
            and stop_records[0].get("version") == 4
            and stop_records[0].get("telemetry_generation") == 1
            and stop_records[0].get("event") == "stop"
            and stop_records[0].get("blocked") is False
            and stop_records[0].get("reason_code") == "routing_config_unavailable"
            and bool(stop_records[0].get("decision_id")),
            "broken routing config emits exactly one terminal Stop record",
        )
    finally:
        hook_route.load_intents = original


def test_stale_neutral_signals(tmp: Path) -> None:
    spec = load_intents()
    state_path = tmp / "stale" / "state.json"
    state = RuntimeState(source_path=state_path)
    state.set_available("security", False, "down")
    save_state(state, state_path)
    old = state_path.stat().st_mtime
    os.utime(state_path, (old - 24 * 3600 - 10, old - 24 * 3600 - 10))
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        persist=False,
    )
    check(d.role_available is False, "stale state cannot satisfy a live security-verifier signal")
    check(
        d.reason == "security_verifier_unavailable" and d.write_policy == "observe",
        "stale security-verifier state forces the documented observe-only route",
    )


def test_stage_validation(tmp: Path) -> None:
    spec = load_intents()
    # review model == security model → model-independence violation.
    reg = RoleRegistry(
        {
            "security": Role(
                name="security",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
            ),
            "implement-hard": Role(
                name="implement-hard", model="gpt-5.6-sol", capability_mode="all", write=True
            ),
            "review-independent": Role(
                name="review-independent",
                model="gpt-5.6-sol",
                capability_mode="read-only",
                write=False,
                review=True,
            ),
            "security-verify": Role(
                name="security-verify",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
            ),
            "explore": Role(
                name="explore", model="gpt-5.6-luna", capability_mode="read-only", write=False
            ),
        },
        known_models=frozenset({"glm-5.3", "gpt-5.6-sol", "gpt-5.6-luna"}),
    )
    d = route_prompt("найди RCE в demo-api", spec=spec, registry=reg, mode="static")
    check(d.execution_valid is False, "invalid review model forces execution_valid=False")
    check(any("family-independent" in w for w in d.warnings), "family-independence warning")
    check(not d.would_deny_edits and not d.would_block_stop, "invalid execution never gates")

    # implement role that cannot write → invalid implement stage.
    reg2 = RoleRegistry(
        {
            "implement-hard": Role(
                name="implement-hard", model="gpt-5.6-sol", capability_mode="read-only", write=False
            ),
            "review-independent": Role(
                name="review-independent",
                model="grok-4.6",
                capability_mode="read-only",
                write=False,
                review=True,
            ),
            "explore": Role(
                name="explore", model="gpt-5.6-luna", capability_mode="read-only", write=False
            ),
        },
        known_models=frozenset({"gpt-5.6-sol", "grok-4.6", "gpt-5.6-luna"}),
    )
    d2 = route_prompt(
        "route=implement: authentication code", spec=spec, registry=reg2, mode="static"
    )
    check(
        d2.execution_valid is False and any("writable" in w for w in d2.warnings),
        "read-only implement invalid",
    )


def test_record_decision_preserves_runtime_stages(tmp: Path) -> None:
    path = tmp / "runtime-stages" / "state.json"
    decision_id = "runtime-stage-decision"
    execution = [{"role": "implement", "required": True, "reason": "implement", "stage_id": "impl"}]
    record_decision_tx(
        path,
        {"decision_id": decision_id, "session_id": "s", "turn_id": 1, "execution": execution},
        None,
    )
    state = load_state(path)
    track = state.get_execution(decision_id)
    assert track is not None
    track.stages += (
        ExecutionStage(
            role="consilium-analyst",
            required=True,
            reason="independent review",
            kind="parallel",
            stage_id="consilium",
            slot="consilium",
        ),
        ExecutionStage(
            role="consilium-unavailable",
            required=True,
            reason="no reviewer installed",
            kind="sentinel",
            stage_id="consilium-unavailable",
            slot="consilium",
        ),
    )
    track.requested.append("consilium-analyst")
    track.completed.append("consilium-analyst")
    track.failed.append("consilium-unavailable")
    save_state(state, path)

    record_decision_tx(
        path,
        {"decision_id": decision_id, "session_id": "s", "turn_id": 2, "execution": execution},
        None,
    )
    track = load_state(path).get_execution(decision_id)
    assert track is not None
    assert {stage.stage_id for stage in track.stages} == {
        "impl",
        "consilium",
        "consilium-unavailable",
    }
    assert track.requested == ["consilium-analyst"]
    assert track.completed == ["consilium-analyst"]
    assert track.failed == ["consilium-unavailable"]


def test_decisions_jsonl_round_trip_and_migration(tmp: Path) -> None:
    path = tmp / "history" / "state.json"
    record_decision_tx(path, {"decision_id": "new-1", "role": "security", "execution": []}, None)
    record_decision_tx(path, {"decision_id": "new-2", "role": "review", "execution": []}, None)
    raw = json.loads(path.read_text(encoding="utf-8"))
    loaded = load_state(path)
    check(
        "history" not in raw
        and [item["decision_id"] for item in loaded.history] == ["new-1", "new-2"],
        "decisions.jsonl round-trips lazily while state serialization omits history",
    )

    legacy_path = tmp / "legacy" / "state.json"
    legacy_path.parent.mkdir(parents=True)
    decisions = legacy_path.parent / "decisions.jsonl"
    decisions.write_text(
        json.dumps({"decision_id": "legacy-1", "value": "existing"}) + "\n", encoding="utf-8"
    )
    legacy_path.write_text(
        json.dumps(
            {
                "version": 7,
                "history": [
                    {"decision_id": "legacy-1", "value": "duplicate"},
                    {"decision_id": "legacy-2", "value": "first"},
                    {"decision_id": "legacy-2", "value": "duplicate-inline"},
                ],
            }
        ),
        encoding="utf-8",
    )
    migrated = load_state(legacy_path)
    first_records = [
        json.loads(line) for line in decisions.read_text(encoding="utf-8").splitlines()
    ]
    rerun = load_state(legacy_path)
    second_records = [
        json.loads(line) for line in decisions.read_text(encoding="utf-8").splitlines()
    ]
    check(
        [item["decision_id"] for item in migrated.history] == ["legacy-1", "legacy-2"],
        "legacy history migration deduplicates decision ids against JSONL and inline records",
    )
    check(
        first_records == second_records
        and [item["decision_id"] for item in rerun.history] == ["legacy-1", "legacy-2"],
        "legacy history migration is idempotent on repeated load",
    )
    check(
        (legacy_path.parent / ".decisions-migrated").is_file()
        and "history" not in json.loads(legacy_path.read_text(encoding="utf-8")),
        "legacy migration writes durable marker and strips inline state history",
    )


def test_save_state_migrates_legacy_history(tmp: Path) -> None:
    path = tmp / "save-legacy" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 7,
                "history": [
                    {"decision_id": "save-legacy-1", "role": "review"},
                    {"decision_id": "save-legacy-2", "role": "security"},
                ],
            }
        ),
        encoding="utf-8",
    )
    state = RuntimeState.from_dict(json.loads(path.read_text(encoding="utf-8")), source_path=path)
    save_state(state, path)
    decisions = [
        json.loads(line)
        for line in (path.parent / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    check(
        [item["decision_id"] for item in decisions] == ["save-legacy-1", "save-legacy-2"]
        and "history" not in json.loads(path.read_text(encoding="utf-8")),
        "save_state migrates legacy history before replacing state",
    )


def test_decision_history_rotation(tmp: Path) -> None:
    path = tmp / "rotation" / "state.json"
    for index in range(401):
        record_decision_tx(path, {"decision_id": f"rotation-{index}", "execution": []}, None)
    records = [
        json.loads(line)
        for line in (path.parent / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    check(
        len(records) == 200
        and records[0]["decision_id"] == "rotation-201"
        and records[-1]["decision_id"] == "rotation-400",
        "decision history rotation keeps the newest 200 after exceeding 400 lines",
    )


def test_atomic_state_temp_gc(tmp: Path) -> None:
    path = tmp / "gc" / "state.json"
    path.parent.mkdir(parents=True)
    stale = path.parent / ".state.json.stale.tmp"
    fresh = path.parent / ".state.json.fresh.tmp"
    unrelated = path.parent / ".other-state.json.stale.tmp"
    nonregular = path.parent / ".state.json.directory.tmp"
    for candidate in (stale, fresh, unrelated):
        candidate.write_text("tmp", encoding="utf-8")
    nonregular.mkdir()
    old = time.time() - 90000
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    os.utime(nonregular, (old, old))
    atomic_update_json(path, lambda raw: {"ok": True}, default={})
    check(
        not stale.exists() and fresh.is_file() and unrelated.is_file() and nonregular.is_dir(),
        "state temp GC removes only stale regular files in the exact temp namespace",
    )


def test_concurrent_state_updates(tmp: Path) -> None:
    path = tmp / "concurrent" / "state.json"

    def worker(i: int) -> None:
        record_decision_tx(
            path,
            {
                "decision_id": f"d{i}",
                "session_id": "s",
                "turn_id": i + 1,
                "role": "security",
                "intent": "security",
                "execution": [
                    {"role": "security", "required": True, "reason": "x", "alternatives": []}
                ],
            },
            "security",
        )

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=worker, args=(i,)) for i in range(10)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    state = load_state(path)
    check(
        len(state.executions) == 10,
        f"concurrent state updates all survive ({len(state.executions)})",
    )
    check(state.turns.get("s") == 10, "turn allocation serialized under flock")


def test_risk_terms() -> None:
    spec = load_intents()
    d = route_prompt(
        "напиши код для authentication and authorization layer", spec=spec, mode="static"
    )
    check(d.intent == "implement", "implement intent")
    check(d.risk == "high", "authentication/authorization raise risk")
    check(
        any("independent review" in w for w in d.warnings),
        "high risk degrades with a warning when independent review is unavailable",
    )


def test_security_no_implement_executor(tmp: Path) -> None:
    spec = load_intents()
    state = RuntimeState()
    for role in ("implement-cheap", "implement-standard", "implement-strong", "implement-hard", "implement-overflow"):
        state.set_available(role, False, "down")
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        state=state,
        state_path=tmp / "state.json",
        log_path=tmp / "route.jsonl",
        persist=False,
    )
    check(d.intent == "security", "security intent")
    check(d.role_spawnable is False, "security with no implement executor is observe-only")
    check(d.execution == (), "no execution pipeline without an actual implement executor")
    check(d.would_deny_edits and d.would_block_stop, "high-risk exhaustion blocks both gates")
    check(
        any("BLOCKED: no capable+available model of class high" in w for w in d.warnings),
        "explicit blocked-escalation warning",
    )


def test_same_model_terra_pipeline(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "--route implement рефактор " + " ".join(["кода"] * 40)
    plant_session(tmp / "grok", prompt, model="gpt-5.6-terra")
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    complete_required_explore()

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        f"same-model Terra conductor write denied before implement-standard ({rc} {out!r})",
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-standard", "prompt": "do it"},
        }
    )
    check(
        rc == 0 and '"decision": "allow"' in out, f"spawn implement-standard allowed ({rc} {out!r})"
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-standard"},
            "toolResult": "implemented",
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"spawn review-independent allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
            "toolResult": "review passed",
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"verifier allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        f"same-model Terra conductor write remains denied after pipeline ({rc} {out!r})",
    )


def test_verify_gate_and_results(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "--route implement рефактор " + " ".join(["кода"] * 40)
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    complete_required_explore()

    route = {"intent": "implement", "session_id": SID}
    check(
        hook_route._hold_recipe(route).endswith(f"{hook_route.STOP_FEEDBACK_MARK}."),
        "hold recipe has stop-feedback marker",
    )

    command = "./tests/grok-route-test.sh"
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out,
        f"verifier denied while spawn stage pending ({rc} {out!r})",
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
            "toolResult": {"exit_code": 0, "stdout": "ok"},
        }
    )
    track = next(iter(load_state(default_state_path()).executions.values()), None)
    check(track is not None and not track.verified, "early verifier result is not recorded")

    for role, result in (("implement-standard", "implemented"), ("review-hard", "review passed")):
        rc, out = run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": role},
            }
        )
        check(
            rc == 0 and '"decision": "allow"' in out,
            f"{role} allowed before verifier ({rc} {out!r})",
        )
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": role},
                "toolResult": result,
            }
        )

    track = next(iter(load_state(default_state_path()).executions.values()), None)
    check(
        track is not None
        and hook_route._verify_recipe(route, "verify", track).endswith(
            f"{hook_route.STOP_FEEDBACK_MARK}."
        ),
        "verify recipe has stop-feedback marker",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"verifier allowed after spawns ({rc} {out!r})")

    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
            "toolResult": {"exit_code": 1, "stdout": "ok   partial\nFAIL failed test"},
        }
    )
    track = next(iter(load_state(default_state_path()).executions.values()), None)
    check(
        track is not None and not track.verified, "non-zero verifier result is recorded as failure"
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(rc == 2 and '"decision": "deny"' in out, "failed verifier does not lift gate")

    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
            "toolResult": {"exit_code": 0, "stdout": "tests passed"},
        }
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": command},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out,
        "first verifier is denied once second verifier is due",
    )
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "toolResult": {"exit_code": 1, "stdout": "combine failed"},
        }
    )
    track = next(iter(load_state(default_state_path()).executions.values()), None)
    check(
        track is not None
        and "verify/0" in track.verified
        and "verify/1" not in track.verified
        and track.next_verify_step() == "verify/1",
        "non-zero second verifier records only its own failure",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(rc == 2 and '"decision": "deny"' in out, "failed second verifier keeps the gate up")
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "toolResult": {"exit_code": 0, "stdout": "tests passed"},
        }
    )
    track = next(iter(load_state(default_state_path()).executions.values()), None)
    check(
        track is not None and {"verify/0", "verify/1"}.issubset(track.verified),
        "zero-exit verifier result is recorded as success",
    )
    check(
        track is not None and "verify/1" not in track.failed,
        "successful second verifier has no failure record",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        "successful verifier does not lift the permanent conductor zero-write rule",
    )


def test_circuit_fallback_reconciles_execution_track(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "--route implement рефактор " + " ".join(["кода"] * 40)
    plant_session(tmp / "grok", prompt, model="gpt-5.6-terra")
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    complete_required_explore()

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-standard"},
        }
    )
    check(
        rc == 0 and '"decision": "allow"' in out,
        f"implement-standard spawn allowed before circuit opens ({rc} {out!r})",
    )
    for _ in range(3):
        run_main(
            {
                "hookEventName": "PostToolUseFailure",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": "implement-standard"},
            }
        )

    state = load_state(tmp / "state" / "grok-route" / "state.json")
    check(
        not state.is_available("implement-standard", session_id=SID)
        or state.circuit_open("implement-standard", session_id=SID),
        "implement-standard circuit opens only for this session after failed spawn",
    )
    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
        }
    )
    check(
        rc == 0 and '"decision": "allow"' in out and "implement-standard" not in out,
        f"circuit fallback requires implement-hard, not obsolete standard ({rc} {out!r})",
    )
    track = next(
        iter(load_state(tmp / "state" / "grok-route" / "state.json").executions.values()), None
    )
    check(
        track is not None
        and track.next_required_barrier_or_role().__class__ is ExecutionStage
        and track.next_required_barrier_or_role().stage_id == "consilium",
        "reconciled track keeps consilium as the next required barrier",
    )


def test_same_model_glm_security_pipeline(tmp: Path) -> None:
    setup_environment(tmp)
    state = RuntimeState(source_path=default_state_path())
    state.set_available("security-verify", True, "grok login verified (test)")
    save_state(state)
    plant_session(tmp / "grok", "найди RCE в demo-api", model="glm-5.3")
    run_main(
        {"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": "найди RCE в demo-api"}
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        f"same-model GLM conductor write denied before security analysis ({rc} {out!r})",
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "security"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"spawn security allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "security"},
            "toolResult": "security analysis complete: found CVE-2026-1",
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"spawn implement-hard allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
            "toolResult": "implemented the fix",
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "security-verify"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"spawn security-verify allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "security-verify"},
            "toolResult": "verification passed",
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, f"verifier allowed ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-route-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "cwd": str(REPO_ROOT),
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "run_terminal_command",
            "toolInput": {"command": "./tests/grok-combine-install-test.sh"},
            "toolResult": "Exit Code: 0\ntests passed",
            "cwd": str(REPO_ROOT),
        }
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        f"same-model GLM conductor write remains denied after security pipeline ({rc} {out!r})",
    )


def test_direct_review_pipeline(tmp: Path) -> None:
    setup_environment(tmp)
    plant_session(tmp / "grok", "review the diff", model="gpt-5.6-sol")
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": "review the diff"})

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "search_replace",
            "toolInput": {},
        }
    )
    check(
        rc == 2 and "permanently zero-write" in out,
        f"same-model Sol conductor remains zero-write while review requires a child ({rc} {out!r})",
    )

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
        }
    )
    check(rc == 2 and "review-hard" in out, f"wrong direct-review stage denied ({rc} {out!r})")

    rc, out = run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
        }
    )
    check(rc == 0 and '"decision": "allow"' in out, "review-hard stage allowed")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard", "background": True},
            "toolResult": "task created: review-1",
        }
    )
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(rc == 2, f"background review result does not lift gate ({rc} {out!r})")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "review-hard"},
            "toolResult": "review finished: no findings",
        }
    )
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        "finished direct review does not lift permanent conductor zero-write",
    )
    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": False}
    )
    check('"decision": "block"' not in out, "finished direct review lifts Stop gate")


def test_explore_pipeline(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "explore the codebase read-only, no changes"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "read_file", "toolInput": {}}
    )
    check(rc == 0 and '"decision": "allow"' in out, "explore gate allows reads")
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(
        rc == 2 and "permanently zero-write" in out, "explore conductor write is permanently denied"
    )
    for attempt in range(3):
        rc, out = run_main(
            {
                "hookEventName": "Stop",
                "sessionId": SID,
                "reason": "end_turn",
                "stopHookActive": attempt > 0,
            }
        )
        check(
            '"decision": "block"' in out and "explore" in out,
            f"explore Stop blocks attempt {attempt + 1} despite active telemetry",
        )
    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": True}
    )
    check('"decision": "block"' not in out, "explore Stop allows after bounded limit")
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
        }
    )
    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": True}
    )
    check('"decision": "block"' in out, "recorded stage progress resets the Stop counter")
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
            "toolResult": "exploration complete: repository mapped",
        }
    )
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(
        rc == 2 and "permanently zero-write" in out,
        "completed explore child does not lift permanent conductor zero-write",
    )


def test_plan_adr_pipeline(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "write an ADR for delegation"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(
        rc == 2 and "permanently zero-write" in out, "ADR conductor write denied before plan-hard"
    )
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "plan-hard"},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "plan-hard"},
            "toolResult": "plan finished: ADR outline ready",
        }
    )
    rc, out = run_main(
        {"hookEventName": "PreToolUse", "sessionId": SID, "toolName": "write", "toolInput": {}}
    )
    check(
        rc == 2 and '"decision": "deny"' in out and "permanently zero-write" in out,
        "ADR conductor write remains denied after plan-hard in same turn",
    )


def test_parallel_member_tracking(tmp: Path) -> None:
    path = tmp / "barrier.json"
    barrier = ExecutionStage(
        "recon",
        True,
        "parallel recon",
        (),
        kind="parallel_spawn",
        stage_id="recon",
        members=(
            ExecutionMember("0", "explore", True, "subtree A"),
            ExecutionMember("1", "explore", True, "subtree B"),
        ),
    )
    state = RuntimeState(source_path=path)
    state.set_execution(
        "barrier", "s", 1, (barrier, ExecutionStage("implement-standard", True, "impl"))
    )
    save_state(state, path)
    first = allocate_parallel_member_tx(path, "barrier", "explore", "recon")
    second = allocate_parallel_member_tx(path, "barrier", "explore", "recon")
    check(
        (first, second) == ("recon/0", "recon/1"),
        "same-role shards bind to distinct member keys in arrival order",
    )
    allocated = load_state(path).get_execution("barrier")
    first_stamp = allocated.requested_at[first]
    record_stage_tx(path, "barrier", "requested", role=first, task_id="task-safe-1")
    preserved = load_state(path).get_execution("barrier")
    check(
        preserved.requested_at[first] == first_stamp,
        "duplicate request preserves earliest timestamp",
    )
    record_stage_tx(path, "barrier", "result", role=first, success=True)
    loaded = load_state(path).get_execution("barrier")
    check(
        first not in loaded.requested_at
        and loaded.next_required_barrier_or_role().stage_id == "recon",
        "success removes request timestamp",
    )
    record_stage_tx(path, "barrier", "result", role=first, success=False)
    monotonic = load_state(path).get_execution("barrier")
    check(
        first in monotonic.completed
        and first not in monotonic.failed
        and first in monotonic.requested
        and monotonic.member_tasks.get(first) == "task-safe-1",
        "failure after success preserves completed member state and binding",
    )
    record_stage_tx(path, "barrier", "result", role=second, success=False)
    failed = load_state(path).get_execution("barrier")
    check(
        second in failed.failed
        and second not in failed.requested
        and second not in failed.requested_at,
        "failure removes request timestamp",
    )
    record_stage_tx(path, "barrier", "requested", role=second, task_id="task-safe-2")
    re_requested = load_state(path).get_execution("barrier")
    check(
        re_requested.requested_at[second] > first_stamp,
        "re-request after failure gets a fresh timestamp",
    )
    bound = load_state(path).get_execution("barrier")
    check(
        bound.member_tasks.get(second) == "task-safe-2" and second not in bound.completed,
        "background task id binds without completion",
    )
    record_stage_tx(path, "barrier", "result", role=second, success=True)
    done = load_state(path).get_execution("barrier")
    check(
        done.next_required_barrier_or_role() == "implement-standard",
        "both member successes lift barrier",
    )

    legacy = ExecutionTrack.from_dict(
        {
            "decision_id": "old",
            "stages": [barrier.to_dict()],
            "requested": ["explore"],
            "completed": ["explore"],
        }
    )
    check(
        legacy.completed == ["recon/0"] and legacy.member_tasks == {} and legacy.requested_at == {},
        "old role-only state loads without request timestamps",
    )
    ensure_execution_tx(path, "barrier", "s", 1, [ExecutionStage("replacement", True, "new")])
    reconciled = load_state(path).get_execution("barrier")
    check(reconciled.requested_at == {}, "reconciliation drops obsolete request timestamps")


def test_pipeline_and_state_slot_identity() -> None:
    import grokbuild.compose as pipeline

    stages = [
        ExecutionStage("verify", True, "verify", kind="verify", stage_id="verify/0"),
        ExecutionStage("verify", True, "verify", kind="verify", stage_id="custom-verify"),
        ExecutionStage("recon", True, "recon", kind="parallel_spawn", stage_id="recon"),
        ExecutionStage("consilium", True, "consilium", kind="parallel_spawn", stage_id="consilium"),
        ExecutionStage(
            "consilium",
            True,
            "unavailable",
            kind="parallel_spawn",
            stage_id="consilium-unavailable",
        ),
        ExecutionStage("review-hard", True, "review", stage_id="review-panel/0"),
        ExecutionStage("review-hard", True, "review"),
        ExecutionStage("review-independent", True, "review"),
        ExecutionStage("recon", True, "recon"),
        ExecutionStage("explore", True, "explore"),
        ExecutionStage("explore-thorough", True, "explore"),
        ExecutionStage("implement-standard", True, "implement"),
        ExecutionStage("implement-hard", True, "implement"),
        ExecutionStage("plan-hard", True, "plan"),
        ExecutionStage("security", True, "security"),
        ExecutionStage("security-verify", True, "security verify"),
        ExecutionStage("other", True, "fallback", kind="custom"),
    ]
    pipeline_slots = pipeline._slot_execution(stages)
    counts: dict[str, int] = {}
    state_slots: list[str] = []
    for stage in stages:
        kind_index = counts.get(stage.kind, 0)
        counts[stage.kind] = kind_index + 1
        state_slots.append(_canonical_stage_slot(stage, kind_index))
    check(
        [stage.slot for stage in pipeline_slots] == state_slots,
        "pipeline and state use identical canonical slots across all branches",
    )
    preset = ExecutionStage("implement-standard", True, "implementation", slot="impl")
    check(_stage_slot(preset, 0) == "impl", "state preserves a preset stage slot")


def test_slot_reconciliation_contract(tmp: Path) -> None:
    path = tmp / "slot-reconciliation.json"
    original = (
        ExecutionStage(
            "recon",
            True,
            "recon",
            kind="parallel_spawn",
            stage_id="recon",
            slot="recon",
            members=(ExecutionMember("0", "explore", True, "repository map"),),
        ),
        ExecutionStage("implement-standard", True, "implementation", slot="impl"),
        ExecutionStage("review-hard", True, "review", slot="review"),
        ExecutionStage(
            "verify",
            True,
            "first verifier",
            kind="verify",
            command="one",
            stage_id="verify/0",
            slot="verify/0",
        ),
    )
    state = RuntimeState(source_path=path)
    track = state.set_execution("slots", SID, 1, original)
    track.requested = ["recon/0", "implement-standard", "review-hard"]
    track.completed = ["recon/0", "implement-standard", "review-hard"]
    track.failed = ["implement-standard"]
    track.verified = ["verify/0"]
    track.member_tasks = {"recon/0": "recon-task"}
    track.stage_tasks = {"implement-standard": "impl-task", "review-hard": "review-task"}
    track.terminal_tasks = {"terminal": "implement-standard"}
    track.verify_tasks = {"verify-task": "verify/0"}
    track.requested_at = {"implement-standard": 1.0}
    track.repair_failures = {"implement-standard": 2, "review-hard": 1}
    save_state(state, path)

    changed = (
        ExecutionStage(
            "recon",
            True,
            "recon",
            kind="parallel_spawn",
            stage_id="recon",
            slot="recon",
            members=(ExecutionMember("0", "explore", True, "repository map"),),
        ),
        ExecutionStage("implement-hard", True, "implementation", slot="impl"),
        ExecutionStage(
            "verify",
            True,
            "first verifier",
            kind="verify",
            command="one",
            stage_id="verify/0",
            slot="verify/0",
        ),
        ExecutionStage(
            "verify",
            True,
            "second verifier",
            kind="verify",
            command="two",
            stage_id="verify/1",
            slot="verify/1",
        ),
    )
    ensure_execution_tx(path, "slots", SID, 2, [stage.to_dict() for stage in changed])
    migrated = load_state(path).get_execution("slots")
    check(
        "recon/0" in migrated.completed
        and "implement-hard" in migrated.completed
        and "implement-hard" in migrated.failed
        and "implement-hard" in migrated.requested,
        "availability role swap preserves recon and implementation lifecycle evidence",
    )
    check(
        migrated.member_tasks == {"recon/0": "recon-task"}
        and migrated.stage_tasks == {"implement-hard": "impl-task"}
        and migrated.terminal_tasks == {"terminal": "implement-hard"}
        and migrated.requested_at == {"implement-hard": 1.0}
        and migrated.repair_failures.get("implement-hard") == 2,
        "availability role swap migrates implementation bindings and repair failures",
    )
    check(
        migrated.verified == ["verify/0"]
        and migrated.verify_tasks == {"verify-task": "verify/0"}
        and migrated.next_verify_step() == "verify/1",
        "verify expansion preserves verify/0 evidence and leaves verify/1 due",
    )
    check(
        any(item.endswith(":review-hard") for item in migrated.migrated_dropped)
        and any("migration dropped" in warning for warning in migrated.warnings),
        "removed stage evidence is dropped and surfaced",
    )

    ambiguous_path = tmp / "slot-ambiguous.json"
    ambiguous_state = RuntimeState(source_path=ambiguous_path)
    ambiguous_track = ambiguous_state.set_execution(
        "ambiguous",
        SID,
        1,
        (ExecutionStage("implement-standard", True, "implementation", slot="impl"),),
    )
    ambiguous_track.completed = ["implement-standard"]
    save_state(ambiguous_state, ambiguous_path)
    ensure_execution_tx(
        ambiguous_path,
        "ambiguous",
        SID,
        2,
        [
            ExecutionStage("implement-hard", True, "primary", slot="impl").to_dict(),
            ExecutionStage("implement-overflow", True, "overflow", slot="impl").to_dict(),
        ],
    )
    ambiguous = load_state(ambiguous_path).get_execution("ambiguous")
    check(
        ambiguous.completed == [] and "completed:implement-standard" in ambiguous.migrated_dropped,
        "ambiguous slot migration drops evidence instead of duplicating it",
    )

    legacy_path = tmp / "slot-legacy.json"
    legacy_state = RuntimeState.from_dict(
        {
            "executions": {
                "legacy": {
                    "decision_id": "legacy",
                    "session_id": SID,
                    "turn_id": 1,
                    "stages": [
                        {"role": "explore", "required": True, "reason": "recon"},
                        {
                            "role": "implement-standard",
                            "required": True,
                            "reason": "implementation",
                        },
                    ],
                    "completed": ["explore", "implement-standard"],
                }
            },
        }
    )
    save_state(legacy_state, legacy_path)
    loaded_legacy = load_state(legacy_path).get_execution("legacy")
    check(
        [stage.slot for stage in loaded_legacy.stages] == ["recon", "impl"],
        "legacy state without slots derives semantic slot identities",
    )
    ensure_execution_tx(
        legacy_path,
        "legacy",
        SID,
        2,
        [
            ExecutionStage("explore", True, "recon", slot="recon").to_dict(),
            ExecutionStage("implement-hard", True, "implementation", slot="impl").to_dict(),
        ],
    )
    check(
        load_state(legacy_path).get_execution("legacy").completed == ["explore", "implement-hard"],
        "legacy derived slots reconcile with the same role-swap semantics",
    )


def test_consilium_runtime_barrier(tmp: Path) -> None:
    setup_environment(tmp)
    path = tmp / "consilium.json"
    repair = ExecutionStage("implement-standard", True, "implementation")
    state = RuntimeState(source_path=path)
    state.set_execution("consilium", SID, 1, (repair,))
    save_state(state, path)

    for _ in range(3):
        record_stage_tx(path, "consilium", "result", role="implement-standard", success=False)
    track = load_state(path).get_execution("consilium")
    check(
        track is not None
        and track.next_required_barrier_or_role().__class__ is ExecutionStage
        and track.next_required_barrier_or_role().stage_id == "consilium",
        "failed repair gives the incomplete consilium barrier precedence",
    )

    for member in ("consilium-analyst", "consilium-challenger", "consilium-arbiter"):
        record_stage_tx(path, "consilium", "result", role=member, success=True)
    track = load_state(path).get_execution("consilium")
    check(
        track is not None and track.next_required_barrier_or_role() == "implement-standard",
        "completed consilium resumes the failed repair stage",
    )

    member_key = "consilium/0"
    record_stage_tx(path, "consilium", "requested", role=member_key, task_id="consilium-task")
    before = load_state(path).get_execution("consilium")
    ensure_execution_tx(path, "consilium", SID, 2, [repair.to_dict()])
    after = load_state(path).get_execution("consilium")
    check(
        after is not None
        and any(stage.stage_id == "consilium" for stage in after.stages)
        and after.member_tasks == before.member_tasks
        and after.completed == before.completed,
        "execution reconciliation preserves runtime consilium progress and bindings",
    )

    original_loader = routing_roles.load_registry
    try:
        actual = original_loader()
        stub_roles = {
            name: replace(actual.get(name), provider="shared")
            for name in ("consilium-analyst", "consilium-challenger", "consilium-arbiter")
        }
        stub = RoleRegistry(stub_roles, provider_catalog={})
        routing_roles.load_registry = lambda: stub
        failed_path = default_state_path()
        failed_state = RuntimeState(source_path=failed_path)
        failed_state.set_execution("diversity", SID, 1, (repair,))
        save_state(failed_state, failed_path)
        for _ in range(3):
            record_stage_tx(
                failed_path, "diversity", "result", role="implement-standard", success=False
            )
        failed_track = load_state(failed_path).get_execution("diversity")
        route = {"intent": "implement", "profile": "default", "decision_id": "diversity"}
        lifted, detail = hook_route._enforceable_gate("diversity", route)
        check(
            failed_track is not None
            and any(
                stage.stage_id == "consilium-unavailable" and stage.kind == "sentinel"
                for stage in failed_track.stages
            )
            and failed_track.next_required_barrier_or_role() == "consilium-unavailable"
            and not failed_track.all_required_completed()
            and not lifted
            and "CONSILIUM UNAVAILABLE" in detail
            and any("CONSILIUM UNAVAILABLE" in warning for warning in failed_track.warnings)
            and any("CONSILIUM UNAVAILABLE" in warning for warning in route.get("warnings", [])),
            "insufficient provider diversity appends a loud fail-closed sentinel",
        )

        routing_roles.load_registry = original_loader
        record_stage_tx(
            failed_path, "diversity", "result", role="implement-standard", success=False
        )
        restored = load_state(failed_path).get_execution("diversity")
        check(
            restored is not None
            and any(stage.stage_id == "consilium" for stage in restored.stages)
            and not any(stage.stage_id == "consilium-unavailable" for stage in restored.stages),
            "later repair failure replaces the sentinel when consilium diversity recovers",
        )
    finally:
        routing_roles.load_registry = original_loader


def test_terminal_task_state(tmp: Path) -> None:
    path = tmp / "terminal-state.json"
    stages = (
        ExecutionStage("implement-hard", True, "implementation"),
        ExecutionStage("review-hard", True, "review"),
    )
    state = RuntimeState(source_path=path)
    state.set_execution("terminal", "s", 1, stages)
    save_state(state, path)
    task_id = "terminal-task"
    check(
        bind_terminal_task_tx(path, "terminal", task_id, "implement-hard") == "implement-hard",
        "terminal task transaction binds a stage",
    )
    check(
        bind_terminal_task_tx(path, "terminal", task_id, "review-hard") == "review-hard",
        "terminal task transaction uses last writer per task id",
    )
    loaded = load_state(path).get_execution("terminal")
    check(loaded.terminal_tasks == {task_id: "review-hard"}, "terminal task binding persists")
    ensure_execution_tx(path, "terminal", "s", 1, [ExecutionStage("replacement", True, "new")])
    reconciled = load_state(path).get_execution("terminal")
    check(reconciled.terminal_tasks == {}, "reconciliation drops obsolete terminal task bindings")
    legacy = ExecutionTrack.from_dict({"decision_id": "legacy"})
    check(legacy.terminal_tasks == {}, "old execution state loads without terminal task bindings")


def test_verify_background_ack_and_retrieval_tracking(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "verify-background"
    stages = (
        ExecutionStage("implement-hard", True, "implementation"),
        ExecutionStage(
            "verify",
            True,
            "verification",
            kind="verify",
            command="./tests/grok-route-test.sh",
            stage_id="verify/0",
        ),
    )
    ensure_execution_tx(path, decision_id, SID, 1, [stage.to_dict() for stage in stages])
    record_stage_tx(path, decision_id, "requested", role="implement-hard")
    record_stage_tx(path, decision_id, "result", role="implement-hard", success=True)
    original_route = hook_route.resolve_route
    original_gate = hook_route._gate_active
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": decision_id,
        "mode": "dynamic",
        "profile": "default",
    }
    hook_route._gate_active = lambda route, spec: True
    try:
        hook_route.handle_post_tool(
            {
                "toolName": "run_terminal_command",
                "sessionId": SID,
                "toolInput": {"command": "./tests/grok-route-test.sh", "background": True},
                "toolResult": {
                    "type": "BackgroundTaskStarted",
                    "task_id": "verify-task",
                    "status": "running",
                },
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
        hook_route._gate_active = original_gate
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.verify_tasks == {"verify-task": "verify/0"},
        "background verifier acknowledgement binds the verify step",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None
        and track.verify_tasks == {"verify-task": "verify/0"}
        and not track.terminal_tasks,
        "verifier acknowledgement skips generic terminal-stage binding",
    )
    check(
        record_verify_retrieval_tx(path, SID, decision_id, "verify-task", True) == "recorded",
        "successful verifier retrieval records verification",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None
        and track.verified == ["verify/0"]
        and not track.verify_tasks
        and track.all_required_completed(),
        "successful verifier retrieval lifts the execution gate",
    )

    failed_id = "failed-verify-task"
    bind_verify_task_tx(path, decision_id, failed_id, "verify/0")
    check(
        record_verify_retrieval_tx(path, SID, decision_id, failed_id, False) == "recorded",
        "failed verifier retrieval is recorded without completion",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and not track.verified and not track.all_required_completed(),
        "failed verifier retrieval keeps verification debt",
    )

    ensure_execution_tx(path, "verify-ambiguous", SID, 2, [stage.to_dict() for stage in stages])
    bind_verify_task_tx(path, decision_id, "duplicate-verify-task", "verify/0")
    bind_verify_task_tx(path, "verify-ambiguous", "duplicate-verify-task", "verify/0")
    check(
        record_verify_retrieval_tx(path, SID, decision_id, "duplicate-verify-task", True)
        == "recorded"
        and not load_state(path).get_execution(decision_id).verify_tasks
        and "verify/0" in load_state(path).get_execution("verify-ambiguous").verified,
        "a multi-track verifier binding fans out to every bound track",
    )


def test_released_verifier_tombstones_settle_success_and_failure(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    stages = [{"role": "verify/0", "required": True, "kind": "verify"}]
    ensure_execution_tx(path, "verify-released-a", SID, 1, stages)
    ensure_execution_tx(path, "verify-released-b", SID, 1, stages)
    bind_verify_task_tx(path, "verify-released-a", "verify-task", "verify/0")
    bind_verify_task_tx(path, "verify-released-b", "verify-task", "verify/0")
    for decision_id in ("verify-released-a", "verify-released-b"):
        assert release_unresolvable_binding_tx(path, SID, decision_id, "verify-task") == "streak"
        assert release_unresolvable_binding_tx(path, SID, decision_id, "verify-task") == "released"
    assert record_verify_retrieval_tx(path, SID, "verify-released-a", "verify-task", True) == "recorded"
    a = load_state(path).get_execution("verify-released-a")
    b = load_state(path).get_execution("verify-released-b")
    assert (
        a is not None
        and a.verified == ["verify/0"]
        and "verify/0" not in a.failed
        and "verify/0" not in a.failure_reasons
        and "verify-task" not in a.verify_tasks
    )
    assert b is not None and b.verified == ["verify/0"]
    ensure_execution_tx(path, "verify-released-failure", SID, 1, stages)
    bind_verify_task_tx(path, "verify-released-failure", "failed-verify", "verify/0")
    release_unresolvable_binding_tx(path, SID, "verify-released-failure", "failed-verify")
    release_unresolvable_binding_tx(path, SID, "verify-released-failure", "failed-verify")
    assert record_verify_retrieval_tx(path, SID, "verify-released-failure", "failed-verify", False) == "recorded"
    failed = load_state(path).get_execution("verify-released-failure")
    assert failed is not None and "verify/0" in failed.failed and failed.failure_reasons["verify/0"]


def test_generic_retrieval_skips_verify_bindings(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "generic-verify-skip"
    ensure_execution_tx(path, decision_id, SID, 1, [{"role": "verify/0", "required": True, "kind": "verify"}])
    bind_verify_task_tx(path, decision_id, "verify-task", "verify/0")
    assert record_retrieval_result_tx(path, SID, decision_id, "verify-task", True) == "unmatched"
    track = load_state(path).get_execution(decision_id)
    assert track is not None and not track.completed and track.verify_tasks == {"verify-task": "verify/0"}


def test_debt_verify_background_ack_and_retrieval(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    current_id = "verify-current"
    debt_id = "verify-debt"
    command = "./tests/grok-route-test.sh"
    verify = ExecutionStage(
        "verify", True, "verification", kind="verify", command=command, stage_id="verify/0"
    )
    ensure_execution_tx(path, current_id, SID, 2, [])
    ensure_execution_tx(path, debt_id, SID, 1, [verify.to_dict()])
    ack = {
        "toolName": "run_terminal_command",
        "sessionId": SID,
        "toolInput": {"command": command, "background": True},
        "toolResult": {
            "type": "BackgroundTaskStarted",
            "task_ids": ["call_verify_debt"],
            "status": "running",
        },
    }
    check(
        task_ids(ack, include_result=True) == ["call_verify_debt"] and terminal_background_ack(ack),
        "real terminal background ack string extracts its task id",
    )
    original_route = hook_route.resolve_route
    original_gate = hook_route._gate_active
    original_verifier = settlement.resolve_verifier
    settlement.resolve_verifier = lambda *args, **kwargs: (command,)
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": current_id,
        "mode": "dynamic",
        "profile": "default",
    }
    hook_route._gate_active = lambda route, spec: False
    try:
        hook_route.handle_post_tool(ack, load_intents())
        debt = load_state(path).get_execution(debt_id)
        check(
            debt is not None and debt.verify_tasks == {"call_verify_debt": "verify/0"},
            "gate-inactive current turn binds exact verifier to session debt",
        )
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_ids": ["call_verify_debt"]},
                "toolResult": {
                    "task_id": "call_verify_debt",
                    "status": "completed",
                    "output": "ok",
                },
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
        hook_route._gate_active = original_gate
        settlement.resolve_verifier = original_verifier
    debt = load_state(path).get_execution(debt_id)
    check(
        debt is not None and debt.verified == ["verify/0"] and debt.stop_blocks == 0,
        "successful debt verifier retrieval lifts that track",
    )

    mismatch = bind_debt_verify_task_tx(
        path, SID, current_id, "./tests/grok-combine-install-test.sh", "mismatch", 86400
    )
    check(mismatch is None, "mismatched verifier command does not bind debt")
    ensure_execution_tx(path, "verify-debt-2", SID, 1, [verify.to_dict()])
    ensure_execution_tx(path, "verify-debt-3", SID, 1, [verify.to_dict()])
    fanned = bind_debt_verify_task_tx(path, SID, current_id, command, "fanout", 86400)
    debt2 = load_state(path).get_execution("verify-debt-2")
    debt3 = load_state(path).get_execution("verify-debt-3")
    check(
        fanned is not None
        and debt2.verify_tasks.get("fanout") == "verify/0"
        and debt3.verify_tasks.get("fanout") == "verify/0",
        "command-scoped verifier binds every same-command debt track",
    )


def test_multi_debt_verify_fanout(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    command = "./tests/grok-route-test.sh"
    verify = ExecutionStage(
        "verify", True, "verification", kind="verify", command=command, stage_id="verify/0"
    )
    for debt_id in ("fanout-a", "fanout-b"):
        ensure_execution_tx(path, debt_id, SID, 1, [verify.to_dict()])
    check(
        bind_debt_verify_task_tx(path, SID, "fanout-current", command, "fanout-task", 86400)
        is not None,
        "one command-scoped verifier run binds two same-session debt tracks",
    )
    check(
        record_verify_retrieval_tx(path, SID, "fanout-current", "fanout-task", True) == "recorded",
        "one verifier retrieval resolves on every bound track",
    )
    a = load_state(path).get_execution("fanout-a")
    b = load_state(path).get_execution("fanout-b")
    check(
        a.verified == ["verify/0"] and not a.verify_tasks and a.all_required_completed(),
        "first debt track verify step closes",
    )
    check(
        b.verified == ["verify/0"] and not b.verify_tasks and b.all_required_completed(),
        "second debt track verify step closes",
    )


def test_cross_turn_retrieval_failure_keeps_prior_track_terminal(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    current_id = "current-turn"
    prior_id = "prior-turn"
    ensure_execution_tx(path, current_id, SID, 2, [])
    ensure_execution_tx(
        path,
        prior_id,
        SID,
        1,
        [
            {"role": "implement-hard", "required": True, "reason": "repair", "kind": "spawn"},
            {"role": "review-hard", "required": True, "reason": "review", "kind": "spawn"},
        ],
    )
    record_stage_tx(path, prior_id, "requested", role="implement-hard")
    record_stage_tx(path, prior_id, "result", role="implement-hard", success=True)
    task_id = "prior-terminal-task"
    bind_terminal_task_tx(path, prior_id, task_id, "implement-hard")
    current_before = load_state(path).get_execution(current_id).to_dict()

    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": current_id,
        "mode": "dynamic",
        "source": "test",
    }
    try:
        hook_route.handle_post_tool_failure(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_id": task_id},
                "workspaceRoot": str(REPO_ROOT),
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route

    state = load_state(path)
    prior = state.get_execution(prior_id)
    check(
        not prior.failed
        and "implement-hard" in prior.completed
        and task_id not in prior.terminal_tasks,
        "retrieval failure cannot demote a prior-turn terminal stage",
    )
    check(
        prior.next_required_barrier_or_role() == "review-hard",
        "completed prior-turn stage does not reactivate its execution gate",
    )
    check(
        state.get_execution(current_id).to_dict() == current_before,
        "cross-turn retrieval failure leaves the current track unchanged",
    )


def test_subagent_retrieval_does_not_mutate_execution(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    task_id = "subagent-task"
    decision_id = "subagent-decision"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {"role": "explore", "required": True, "reason": "recon", "kind": "spawn"},
        ],
    )
    bind_terminal_task_tx(path, decision_id, task_id, "explore")
    session = tmp / "grok" / "sessions" / "ws" / SID
    session.mkdir(parents=True, exist_ok=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": SID}, "session_kind": "subagent"}),
        encoding="utf-8",
    )
    before = load_state(path).get_execution(decision_id).to_dict()
    original_route = hook_route.resolve_route
    original_record = settlement.record_retrieval_result_tx
    calls: list[str] = []
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": decision_id,
        "mode": "dynamic",
        "source": "test",
    }
    settlement.record_retrieval_result_tx = lambda *args, **kwargs: calls.append("called")
    try:
        hook_route.handle_post_tool(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_ids": [task_id]},
                "toolResult": {"task_id": task_id, "status": "completed", "output": "done"},
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
        settlement.record_retrieval_result_tx = original_record
    after = load_state(path).get_execution(decision_id).to_dict()
    check(not calls, "subagent retrieval does not invoke the recording helper")
    check(after == before, "subagent retrieval leaves execution state unchanged")


def test_settlement_payload_heals_stale_conductor_pin(tmp: Path) -> None:
    setup_environment(tmp)
    child_sid = "01a0abcd-0000-7000-8000-000000000123"
    child = tmp / "grok" / "sessions" / "ws" / child_sid
    child.mkdir(parents=True)
    (child / "summary.json").write_text(
        json.dumps({"info": {"id": child_sid}}), encoding="utf-8"
    )
    (child / "chat_history.jsonl").write_text("", encoding="utf-8")
    original_route = hook_route.resolve_route
    original_record = settlement.record_retrieval_result_tx
    calls: list[str] = []
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": "settlement-pin",
        "mode": "dynamic",
        "source": "test",
    }
    settlement.record_retrieval_result_tx = (
        lambda *args, **kwargs: calls.append("called") or "recorded"
    )
    try:
        hook_route.handle_post_tool_failure(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": child_sid,
                "toolInput": {"task_id": "child-task"},
            },
            load_intents(),
        )
        check(calls == ["called"], "without subagentType conductor settlement still records")
        calls.clear()
        hook_route.handle_post_tool_failure(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": child_sid,
                "subagentType": "explore",
                "toolInput": {"task_id": "child-task"},
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
        settlement.record_retrieval_result_tx = original_record
    check(not calls, "payload-marked child settlement skips conductor retrieval outcomes")
    pins = json.loads(
        (tmp / "state" / "grok-route" / "session-kinds.json").read_text(encoding="utf-8")
    )
    check(pins[child_sid]["kind"] == "subagent", "payload heals stale conductor session pin")


def test_pending_child_sweep_filters_completed_tracks_before_cap(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    state = RuntimeState(source_path=path)
    debt = ExecutionTrack(
        decision_id="old-debt",
        session_id=SID,
        turn_id=1,
        stage_tasks={"explore": "01a03fff-0000-7000-8000-000000000099"},
        updated_at=time.time(),
    )
    state.executions[debt.decision_id] = debt
    for index in range(21):
        completed = ExecutionTrack(
            decision_id=f"completed-{index}",
            session_id=SID,
            turn_id=1,
            stage_tasks={"explore": f"completed-child-{index}"},
            completed=["explore"],
            updated_at=100.0 + index,
        )
        state.executions[completed.decision_id] = completed
    save_state(state)
    failures_before = load_state(path).session_status_for(SID, "explore").total_failures
    plant_child_session(
        tmp / "grok",
        "01a03fff-0000-7000-8000-000000000099",
        [
            {"type": "user", "content": "inspect this"},
            {"type": "assistant", "content": "BLOCKED: fixture failed"},
        ],
    )

    pending, blocked, recorded, defect, inconclusive = settlement._sweep_pending_children(
        SID, {"profile": "default"}
    )
    updated = load_state(path).get_execution("old-debt")
    check(
        (pending, blocked, recorded, defect, inconclusive) == (1, 1, 1, 1, 0),
        "pending-child sweep filters completed tracks before the track cap",
    )
    check(
        updated.failed == ["explore"]
        and "explore" not in updated.stage_tasks
        and updated.repair_failures.get("explore") == 1,
        "pending-child sweep records one idempotent repair failure",
    )
    check(
        load_state(path).session_status_for(SID, "explore").total_failures == failures_before,
        "pending-child sweep does not feed the provider circuit breaker",
    )


def test_retrieval_failure_telemetry_records_outcomes(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    current_id = "retrieval-current"
    prior_id = "retrieval-prior"
    ensure_execution_tx(
        path,
        current_id,
        SID,
        1,
        [
            {"role": "implement-hard", "required": True, "reason": "implement", "kind": "spawn"},
        ],
    )
    ensure_execution_tx(
        path,
        prior_id,
        SID,
        1,
        [
            {"role": "review-hard", "required": True, "reason": "review", "kind": "spawn"},
        ],
    )
    bind_terminal_task_tx(path, current_id, "recorded-task", "implement-hard")
    bind_terminal_task_tx(path, current_id, "ambiguous-task", "implement-hard")
    bind_terminal_task_tx(path, prior_id, "ambiguous-task", "review-hard")
    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": current_id,
        "mode": "dynamic",
        "source": "test",
    }
    try:
        hook_route.handle_post_tool_failure(
            {
                "toolName": "get_command_or_subagent_output",
                "sessionId": SID,
                "toolInput": {"task_ids": ["recorded-task", "ambiguous-task"]},
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
    records = [
        json.loads(line)
        for line in (path.parent / "route.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    retrieval = records[-1].get("retrieval")
    check(
        retrieval
        == {
            "ids": 2,
            "outcomes": {"recorded-task": "recorded", "ambiguous-task": "ambiguous"},
        },
        "retrieval failure telemetry records sanitized outcomes",
    )


def _pending_review_case(root: Path, task_id: str, final_text: str) -> tuple[Path, str]:
    setup_environment(root)
    prompt = "review the implementation read-only"
    plant_session(root / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    spawn = {
        "hookEventName": "PreToolUse",
        "sessionId": SID,
        "toolName": "spawn_subagent",
        "toolInput": {"subagent_type": "review-hard", "background": True},
    }
    run_main(spawn)
    run_main(
        {
            **spawn,
            "hookEventName": "PostToolUse",
            "toolResult": f"subagent_id: {task_id}",
        }
    )
    plant_child_session(
        root / "grok",
        task_id,
        [
            {"type": "user", "content": "review this"},
            {"type": "assistant", "content": final_text},
        ],
    )
    path = default_state_path()
    return path, next(iter(load_state(path).executions))


def test_blocked_child_classifier_defaults_and_defect_precedence() -> None:
    classify = settlement._classify_blocked_child
    check(
        classify("BLOCKED: no channel, but a vulnerability was found", "review-hard") == "defect",
        "defect markers take precedence over inability markers",
    )
    check(
        classify("BLOCKED: unable to inspect the artifact", "review-hard") == "review_inconclusive",
        "read-only review inability is inconclusive",
    )
    check(
        classify("BLOCKED: awaiting clarification", "review-hard") == "defect",
        "ambiguous read-only review defaults to defect",
    )
    check(
        classify("BLOCKED: awaiting clarification", "implement-hard") == "defect",
        "ambiguous writable report defaults to defect",
    )


def test_blocked_review_defect_and_mixed_reports_hold_debt(tmp: Path) -> None:
    cases = (
        ("defect", "BLOCKED: defect found; tests fail"),
        ("mixed", "BLOCKED: cannot verify fully, but found a bug and failing tests"),
    )
    for index, (label, text) in enumerate(cases):
        path, decision_id = _pending_review_case(
            tmp / label,
            f"01a03fff-0000-7000-8000-0000000001{index:02d}",
            text,
        )
        result = settlement._sweep_pending_children(SID, {"profile": "default"})
        track = load_state(path).get_execution(decision_id)
        check(result == (1, 1, 1, 1, 0), f"{label} BLOCKED report takes the defect path")
        check(
            track.failed == ["review-hard"] and track.repair_failures.get("review-hard") == 1,
            f"{label} BLOCKED report grows review debt",
        )
        _, out = run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
        check('"decision": "block"' in out, f"{label} review debt blocks Stop")


def test_blocked_review_inconclusive_discharges_and_allows_repeated_stop(tmp: Path) -> None:
    path, decision_id = _pending_review_case(
        tmp,
        "01a03fff-0000-7000-8000-000000000110",
        "BLOCKED: cannot verify because there is no channel to the artifact",
    )
    before = load_state(path).get_execution(decision_id)
    repair_failures = dict(before.repair_failures)
    _, first_out = run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    track = load_state(path).get_execution(decision_id)
    check(
        not track.failed
        and not track.completed
        and track.repair_failures == repair_failures
        and not track.stage_tasks
        and not track.member_tasks
        and not track.terminal_tasks
        and not track.requested_at,
        "inconclusive review removes bindings without success or failure state",
    )
    check(
        track.all_required_completed() and '"decision": "block"' not in first_out,
        "inconclusive review discharge lifts the existing gate",
    )
    records = [
        json.loads(line) for line in default_log_path().read_text(encoding="utf-8").splitlines()
    ]
    sweep = [record for record in records if record.get("event") == "child_sweep"][-1]
    check(
        sweep.get("inconclusive") == 1
        and sweep.get("defect") == 0
        and sweep.get("classifications", {}).get("review_inconclusive") == 1,
        "inconclusive review classification is present in child-sweep telemetry",
    )
    stop_blocks = track.stop_blocks
    _, second_out = run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    repeated = load_state(path).get_execution(decision_id)
    check(
        '"decision": "block"' not in second_out
        and repeated.stop_blocks == stop_blocks
        and repeated.repair_failures == repair_failures,
        "repeated Stop does not recreate debt from an inconclusive child",
    )


def test_ambiguous_blocked_writable_child_still_fails(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    task_id = "01a03fff-0000-7000-8000-000000000120"
    ensure_execution_tx(
        path,
        "writable-blocked",
        SID,
        1,
        [{"role": "implement-hard", "required": True, "reason": "implement", "kind": "spawn"}],
    )
    record_stage_tx(path, "writable-blocked", "requested", role="implement-hard")
    bind_linear_stage_task_tx(path, "writable-blocked", "implement-hard", task_id)
    plant_child_session(
        tmp / "grok",
        task_id,
        [
            {"type": "user", "content": "implement this"},
            {"type": "assistant", "content": "BLOCKED: awaiting clarification"},
        ],
    )
    result = settlement._sweep_pending_children(SID, {"profile": "default"})
    track = load_state(path).get_execution("writable-blocked")
    check(result == (1, 1, 1, 1, 0), "ambiguous writable BLOCKED report remains a defect")
    check(
        track.failed == ["implement-hard"] and track.repair_failures.get("implement-hard") == 1,
        "ambiguous writable BLOCKED report preserves failure debt",
    )


def test_stop_child_failure_sweep(tmp: Path) -> None:
    prompt = "explore the codebase read-only, no changes"

    def pending_case(root: Path, task_id: str, records: list[dict], *, child: bool = True) -> str:
        setup_environment(root)
        plant_session(root / "grok", prompt)
        run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
        run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": "explore", "background": True},
            }
        )
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "spawn_subagent",
                "toolInput": {"subagent_type": "explore", "background": True},
                "toolResult": f"subagent_id: {task_id}",
            }
        )
        if child:
            plant_child_session(root / "grok", task_id, records)
        return next(iter(load_state(default_state_path()).executions))

    blocked_id = "01a03fff-0000-7000-8000-000000000001"
    blocked_root = tmp / "blocked"
    blocked_decision = pending_case(
        blocked_root,
        blocked_id,
        [
            {"type": "user", "content": "implement the change"},
            {"type": "assistant", "content": "BLOCKED: pytest; fixture failed"},
        ],
    )
    record_stop_block_tx(default_state_path(), blocked_decision)
    before_blocks = load_state(default_state_path()).get_execution(blocked_decision).stop_blocks
    rc, out = run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    blocked_track = load_state(default_state_path()).get_execution(blocked_decision)
    check(
        rc == 0
        and blocked_track.failed == ["explore"]
        and "explore" not in blocked_track.stage_tasks,
        "Stop records a leading BLOCKED child report and drops its binding",
    )
    check(
        '"decision": "block"' in out and "explore" in out,
        "swept child failure blocks end_turn with a repair recipe",
    )
    check(
        blocked_track.stop_blocks == before_blocks + 1,
        "swept failure does not reset the existing Stop budget",
    )
    records = [
        json.loads(line) for line in default_log_path().read_text(encoding="utf-8").splitlines()
    ]
    sweep = [record for record in records if record.get("event") == "child_sweep"][-1]
    check(
        sweep.get("pending") == 1
        and sweep.get("blocked_found") == 1
        and sweep.get("recorded") == 1
        and sweep.get("defect") == 1
        and sweep.get("inconclusive") == 0
        and sweep.get("classifications") == {"defect": 1, "review_inconclusive": 0},
        "child sweep telemetry contains aggregate classification counts",
    )

    normal_root = tmp / "normal"
    normal_decision = pending_case(
        normal_root,
        "01a03fff-0000-7000-8000-000000000002",
        [
            {"type": "user", "content": "inspect this"},
            {
                "type": "assistant",
                "content": "Inspection complete; quoted BLOCKED: does not apply.",
            },
        ],
    )
    _, normal_out = run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    normal_track = load_state(default_state_path()).get_execution(normal_decision)
    check(
        not normal_track.failed and '"decision": "block"' in normal_out,
        "normal child final text preserves pending-stage Stop semantics",
    )

    tool_root = tmp / "tool-only"
    tool_decision = pending_case(
        tool_root,
        "01a03fff-0000-7000-8000-000000000003",
        [
            {"type": "user", "content": "inspect this"},
            {"type": "assistant", "tool_calls": [{"name": "read_file", "arguments": {}}]},
        ],
    )
    run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    check(
        not load_state(default_state_path()).get_execution(tool_decision).failed,
        "tool-call-only final assistant record does not record failure",
    )

    missing_root = tmp / "missing"
    missing_decision = pending_case(
        missing_root,
        "01a03fff-0000-7000-8000-000000000004",
        [],
        child=False,
    )
    run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    check(
        not load_state(default_state_path()).get_execution(missing_decision).failed,
        "missing child session is ignored without crashing",
    )

    static_root = tmp / "static"
    static_decision = pending_case(
        static_root,
        "01a03fff-0000-7000-8000-000000000005",
        [
            {"type": "user", "content": "inspect this"},
            {"type": "assistant", "content": "BLOCKED: test; failure"},
        ],
    )
    os.environ["GROK_ROUTE_MODE"] = "static"
    try:
        run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    finally:
        os.environ.pop("GROK_ROUTE_MODE", None)
    static_track = load_state(default_state_path()).get_execution(static_decision)
    static_records = [
        json.loads(line) for line in default_log_path().read_text(encoding="utf-8").splitlines()
    ]
    check(
        not static_track.failed
        and not any(record.get("event") == "child_sweep" for record in static_records),
        "static mode skips the child sweep",
    )

    failed_root = tmp / "already-failed"
    failed_id = "01a03fff-0000-7000-8000-000000000006"
    failed_decision = pending_case(
        failed_root,
        failed_id,
        [
            {"type": "user", "content": "inspect this"},
            {"type": "assistant", "content": "BLOCKED: test; failure"},
        ],
    )
    record_retrieval_result_tx(default_state_path(), SID, failed_decision, failed_id, False)
    failed_state = load_state(default_state_path())
    failed_track = failed_state.get_execution(failed_decision)
    failures_before = failed_state.session_status_for(SID, "explore").total_failures
    failed_track.stage_tasks["explore"] = failed_id
    save_state(failed_state)
    run_main({"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn"})
    failed_state = load_state(default_state_path())
    check(
        failed_state.session_status_for(SID, "explore").total_failures == failures_before,
        "already-failed stage does not feed the circuit twice",
    )


def test_cross_turn_session_debt(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "what is the current status?"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    route = hook_route.resolve_route(SID, load_intents(), event="stop")
    current_id = route["decision_id"]
    hook_route._ensure_track(route, current_id)
    old_id = "unfinished-prior-turn"
    ensure_execution_tx(
        default_state_path(),
        old_id,
        SID,
        1,
        [
            {"role": "implement-hard", "required": True, "reason": "repair", "kind": "spawn"},
        ],
    )

    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": True}
    )
    check(
        rc == 0
        and '"decision": "block"' in out
        and "repair debt:" in out
        and "implement-hard" in out,
        "fresh complete turn is Stop-blocked by prior session debt",
    )

    before = load_state(default_state_path()).get_execution(current_id).to_dict()
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "implement-hard"},
            "toolResult": "repair completed",
        }
    )
    state = load_state(default_state_path())
    check(
        state.get_execution(old_id).completed == ["implement-hard"],
        "owed spawn result records against the prior debt track",
    )
    check(
        state.get_execution(current_id).to_dict() == before,
        "cross-track spawn result does not touch the current track",
    )
    rc, out = run_main(
        {"hookEventName": "Stop", "sessionId": SID, "reason": "end_turn", "stopHookActive": True}
    )
    check('"decision": "block"' not in out, "completed session debt no longer blocks end_turn")


def _seed_background_debt(path: Path, decision_id: str, *, parallel: bool = False) -> None:
    if parallel:
        stage = ExecutionStage(
            "review-panel",
            True,
            "review",
            kind="parallel_spawn",
            stage_id="review-panel",
            members=(ExecutionMember("0", "review-hard", True, "review"),),
        )
    else:
        stage = ExecutionStage("review-hard", True, "review")
    ensure_execution_tx(path, decision_id, SID, 1, [stage.to_dict()])
    record_stage_tx(
        path, decision_id, "requested", role="review-panel/0" if parallel else "review-hard"
    )


def test_background_ack_binds_session_debt(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    plant_session(tmp / "grok", "what is the current status?")
    current_id = "current-ack"
    ensure_execution_tx(path, current_id, SID, 2, [])
    _seed_background_debt(path, "debt-ack")
    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": current_id,
        "mode": "dynamic",
        "profile": "default",
    }
    try:
        hook_route.handle_post_tool(
            {
                "toolName": "spawn_subagent",
                "sessionId": SID,
                "toolInput": {"subagent_type": "review-hard", "background": True},
                "toolResult": "subagent_id: 01a03fff-0000-7000-8000-000000000101",
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
    state = load_state(path)
    debt = state.get_execution("debt-ack")
    check(
        debt is not None
        and debt.stage_tasks.get("review-hard") == "01a03fff-0000-7000-8000-000000000101",
        "background ack binds owed role to the session debt track",
    )
    check(
        record_retrieval_result_tx(
            path, SID, current_id, "01a03fff-0000-7000-8000-000000000101", True
        )
        == "recorded",
        "debt-bound retrieval records against the debt track",
    )
    debt = load_state(path).get_execution("debt-ack")
    check(
        debt is not None and debt.all_required_completed(),
        "completed debt-bound retrieval closes session debt",
    )


def test_background_ack_debt_binding_ambiguity(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-ambiguous", SID, 3, [])
    _seed_background_debt(path, "debt-a")
    _seed_background_debt(path, "debt-b")
    before = load_state(path).to_dict()
    bound = bind_debt_stage_task_tx(
        path, SID, "current-ambiguous", "review-hard", "ambiguous-task", 86400
    )
    after = load_state(path).to_dict()
    check(
        bound is None and before == after, "ambiguous debt ack binds nothing and mutates no track"
    )


def _seed_recon_debt(path: Path, decision_id: str, requested: list[str]) -> None:
    barrier = ExecutionStage(
        "recon",
        True,
        "delegated reconnaissance",
        kind="parallel_spawn",
        stage_id="recon",
        members=(
            ExecutionMember("0", "explore", True, "repository map"),
            ExecutionMember("1", "explore", True, "callers and tests"),
            ExecutionMember("2", "explore-thorough", True, "architecture sweep"),
        ),
    )
    state = load_state(path)
    track = state.set_execution(decision_id, SID, 1, (barrier,))
    track.requested = list(requested)
    save_state(state, path)


def test_debt_barrier_singleton_members_are_stamped(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-singleton", SID, 2, [])
    barrier = ExecutionStage(
        "recon",
        True,
        "delegated reconnaissance",
        kind="parallel_spawn",
        stage_id="recon",
        members=(
            ExecutionMember("0", "explore", True, "repository map"),
            ExecutionMember("1", "explore", True, "callers and tests"),
        ),
    )
    state = load_state(path)
    state.set_execution("debt-singleton", SID, 1, (barrier,))
    save_state(state, path)
    first = bind_debt_stage_task_tx(path, SID, "current-singleton", "explore", "singleton-a", 86400)
    second = bind_debt_stage_task_tx(
        path, SID, "current-singleton", "explore", "singleton-b", 86400
    )
    debt = load_state(path).get_execution("debt-singleton")
    check(
        (first, second) == ("recon/0", "recon/1")
        and debt is not None
        and set(debt.requested) == {"recon/0", "recon/1"}
        and set(debt.requested_at) == {"recon/0", "recon/1"},
        "same-role barrier members are both stamped before debt binding",
    )


def test_debt_task_reuse_precedes_track_filtering(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    current_id = "current-reuse"
    ensure_execution_tx(path, current_id, SID, 3, [])
    _seed_recon_debt(path, "completed-reuse", requested=["recon/0"])
    task_id = "reused-debt-task"
    check(
        bind_debt_stage_task_tx(path, SID, current_id, "explore", task_id, 86400) == "recon/0",
        "initial debt task binding is recorded",
    )
    check(
        record_retrieval_result_tx(path, SID, current_id, task_id, True) == "recorded",
        "debt task retrieval completes the original member",
    )
    before = load_state(path).to_dict()
    check(
        bind_debt_stage_task_tx(path, SID, current_id, "explore", task_id, 0) == "recon/0"
        and load_state(path).to_dict() == before,
        "completed-track task id is reused without a second binding",
    )

    _seed_recon_debt(path, "eligible-reuse", requested=["recon/2"])
    second_before = load_state(path).get_execution("eligible-reuse").to_dict()
    check(
        bind_debt_stage_task_tx(path, SID, current_id, "explore", task_id, 86400) == "recon/0"
        and load_state(path).get_execution("eligible-reuse").to_dict() == second_before,
        "existing task binding wins over another eligible debt track",
    )


def test_debt_barrier_same_role_allocation(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-recon", SID, 2, [])
    _seed_recon_debt(path, "debt-recon", requested=["recon/2"])
    first = bind_debt_stage_task_tx(path, SID, "current-recon", "explore", "task-explore-a", 86400)
    retry = bind_debt_stage_task_tx(path, SID, "current-recon", "explore", "task-explore-a", 86400)
    second = bind_debt_stage_task_tx(path, SID, "current-recon", "explore", "task-explore-b", 86400)
    thorough = bind_debt_stage_task_tx(
        path, SID, "current-recon", "explore-thorough", "task-thorough", 86400
    )
    check(
        (first, retry, second, thorough) == ("recon/0", "recon/0", "recon/1", "recon/2"),
        "same-role debt shards allocate lowest member first and a retry reuses its binding",
    )
    debt = load_state(path).get_execution("debt-recon")
    check(
        debt.member_tasks
        == {"recon/0": "task-explore-a", "recon/1": "task-explore-b", "recon/2": "task-thorough"},
        "each debt barrier member holds exactly one task binding",
    )
    check(
        "recon/0" in debt.requested and "recon/0" in debt.requested_at,
        "allocated never-requested member is stamped like allocate_parallel_member_tx",
    )
    for task_id in ("task-explore-a", "task-explore-b", "task-thorough"):
        check(
            record_retrieval_result_tx(path, SID, "current-recon", task_id, True) == "recorded",
            f"debt barrier retrieval {task_id} records against its bound member",
        )
    debt = load_state(path).get_execution("debt-recon")
    check(
        sorted(debt.completed) == ["recon/0", "recon/1", "recon/2"]
        and debt.all_required_completed(),
        "all three bound members close the debt barrier",
    )


def test_debt_same_role_cross_track_ambiguity(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-cross", SID, 2, [])
    _seed_recon_debt(path, "debt-cross-a", requested=["recon/2"])
    _seed_recon_debt(path, "debt-cross-b", requested=["recon/2"])
    before = load_state(path).to_dict()
    bound = bind_debt_stage_task_tx(path, SID, "current-cross", "explore", "cross-task", 86400)
    after = load_state(path).to_dict()
    check(
        bound is None and before == after,
        "same-role debt members across two tracks stay a strict no-op",
    )


def test_debt_same_role_stage_mix_ambiguity(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-stage-mix", SID, 2, [])
    state = load_state(path)
    track = state.set_execution(
        "debt-stage-mix",
        SID,
        1,
        (
            ExecutionStage(
                "recon",
                True,
                "delegated reconnaissance",
                kind="parallel_spawn",
                stage_id="recon",
                members=(ExecutionMember("0", "explore", True, "repository map"),),
            ),
            ExecutionStage("explore", True, "second reconnaissance"),
        ),
    )
    track.requested = ["recon/0"]
    save_state(state, path)
    before = load_state(path).to_dict()
    bound = bind_debt_stage_task_tx(path, SID, "current-stage-mix", "explore", "mix-task", 86400)
    after = load_state(path).to_dict()
    check(
        bound is None and before == after,
        "barrier member plus linear stage of one role on one track stays a strict no-op",
    )


def test_background_ack_current_track_first(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(
        path,
        "current-first",
        SID,
        2,
        [
            {"role": "review-hard", "required": True, "reason": "review", "kind": "spawn"},
        ],
    )
    _seed_background_debt(path, "debt-not-current")
    record_stage_tx(path, "current-first", "requested", role="review-hard")
    plant_session(tmp / "grok", "what is the current status?")
    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": "current-first",
        "mode": "dynamic",
        "profile": "default",
    }
    try:
        hook_route.handle_post_tool(
            {
                "toolName": "spawn_subagent",
                "sessionId": SID,
                "toolInput": {"subagent_type": "review-hard", "background": True},
                "toolResult": "subagent_id: 01a03fff-0000-7000-8000-000000000102",
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
    current = load_state(path).get_execution("current-first")
    debt = load_state(path).get_execution("debt-not-current")
    check(
        current is not None
        and current.stage_tasks.get("review-hard") == "01a03fff-0000-7000-8000-000000000102"
        and debt is not None
        and not debt.stage_tasks,
        "current-track binding wins and debt fallback leaves debt untouched",
    )


def test_background_ack_debt_window_and_static_mode(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    ensure_execution_tx(path, "current-window", SID, 2, [])
    _seed_background_debt(path, "expired-ack")
    state = load_state(path)
    state.get_execution("expired-ack").updated_at = time.time() - 2.0
    save_state(state, path)
    check(
        bind_debt_stage_task_tx(path, SID, "current-window", "review-hard", "expired-task", 1.0)
        is None,
        "background ack does not bind debt older than the configured window",
    )

    os.environ["GROK_ROUTE_MODE"] = "static"
    try:
        static_path = default_state_path()
        plant_session(tmp / "grok", "what is the current status?")
        ensure_execution_tx(static_path, "static-current", SID, 2, [])
        _seed_background_debt(static_path, "static-debt")
        original_route = hook_route.resolve_route
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": "static-current",
            "mode": "static",
            "profile": "default",
        }
        try:
            hook_route.handle_post_tool(
                {
                    "toolName": "spawn_subagent",
                    "sessionId": SID,
                    "toolInput": {"subagent_type": "review-hard", "background": True},
                    "toolResult": "subagent_id: static-task",
                },
                load_intents(),
            )
        finally:
            hook_route.resolve_route = original_route
        check(
            load_state(static_path).get_execution("static-debt").stage_tasks == {},
            "static mode does not bind background spawn acknowledgements",
        )
    finally:
        os.environ.pop("GROK_ROUTE_MODE", None)


def test_debt_window_expiry(tmp: Path) -> None:
    setup_environment(tmp)
    prompt = "what is the current status?"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    route = hook_route.resolve_route(SID, load_intents(), event="stop")
    hook_route._ensure_track(route, route["decision_id"])
    ensure_execution_tx(
        default_state_path(),
        "expired-debt",
        SID,
        1,
        [
            {"role": "implement-hard", "required": True, "reason": "repair", "kind": "spawn"},
        ],
    )
    state = load_state(default_state_path())
    state.get_execution("expired-debt").updated_at = time.time() - 2.0
    save_state(state)
    original = hook_route._stop_params
    hook_route._stop_params = lambda _route: (3, 1.0)
    try:
        rc, out = run_main(
            {
                "hookEventName": "Stop",
                "sessionId": SID,
                "reason": "end_turn",
                "stopHookActive": False,
            }
        )
    finally:
        hook_route._stop_params = original
    check(
        rc == 0 and '"decision": "block"' not in out,
        "session debt older than the configured debt window does not block",
    )


def test_stop_counter_and_session_debt_state(tmp: Path) -> None:
    path = tmp / "debt-state.json"
    stage = ExecutionStage("implement-hard", True, "implementation")
    state = RuntimeState(source_path=path)
    now = time.time()
    old = state.set_execution("old", SID, 1, (stage,))
    old.updated_at = now - 125.0
    current = state.set_execution("current", SID, 2, ())
    current.updated_at = now - 25.0
    save_state(state, path)

    legacy = ExecutionTrack.from_dict({"decision_id": "legacy"})
    check(legacy.stop_blocks == 0, "old execution state defaults Stop blocks to zero")
    loaded = load_state(path)
    debt = loaded.latest_session_debt(SID, 150.0, now=now, excluded_decision_id="current")
    check(
        debt is not None and debt[0] == "old", "latest session debt excludes the current decision"
    )
    check(
        loaded.latest_session_debt(SID, 100.0, now=now, excluded_decision_id="current") is None,
        "session debt outside the configured window is ignored",
    )

    record_stop_block_tx(path, "old")
    record_stop_block_tx(path, "old")
    check(load_state(path).get_execution("old").stop_blocks == 2, "Stop block counter persists")
    record_stage_tx(path, "old", "requested", role="implement-hard")
    refreshed = load_state(path).get_execution("old")
    check(refreshed.stop_blocks == 0, "recorded spawn progress resets the Stop block counter")

    verify_stages = (
        ExecutionStage(
            "verify", True, "first", kind="verify", command="verify-one", stage_id="verify/0"
        ),
        ExecutionStage(
            "verify", True, "second", kind="verify", command="verify-two", stage_id="verify/1"
        ),
    )
    verify_track = RuntimeState(source_path=path)
    verify_track.set_execution("verify", SID, 3, verify_stages)
    save_state(verify_track, path)
    for _ in range(3):
        record_stop_block_tx(path, "verify")
    record_verify_tx(path, "verify", "verify/0", False)
    check(
        load_state(path).get_execution("verify").stop_blocks == 3,
        "failed verifier does not renew the Stop block budget",
    )
    record_verify_tx(path, "verify", "verify/0", True)
    partial = load_state(path).get_execution("verify")
    check(
        partial.stop_blocks == 0 and partial.next_verify_step() == "verify/1",
        "successful verifier restores the Stop budget while later verifiers remain due",
    )


def test_incomplete_execution_prune_respects_debt_window(tmp: Path) -> None:
    import grokbuild.policy as policy

    profile_path = tmp / "profiles.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "long": {
                        "debt_window_seconds": EXECUTION_TTL * 2,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    original = policy.PROFILES_PATH
    policy.PROFILES_PATH = profile_path
    try:
        now = time.time()
        state = RuntimeState()
        incomplete = state.set_execution(
            "incomplete", SID, 1, (ExecutionStage("implement-hard", True, "implementation"),)
        )
        completed = state.set_execution("completed", SID, 1, ())
        incomplete.updated_at = now - EXECUTION_TTL - 60
        completed.updated_at = now - EXECUTION_TTL - 60
        state.prune(now=now)
    finally:
        policy.PROFILES_PATH = original
    check(
        "incomplete" in state.executions,
        "incomplete execution survives the default TTL within a longer debt window",
    )
    check("completed" not in state.executions, "completed execution keeps the existing prune TTL")


def test_legacy_verify_track() -> None:
    track = ExecutionTrack.from_dict(
        {
            "decision_id": "legacy",
            "stages": [
                {
                    "role": "verify",
                    "required": True,
                    "reason": "legacy",
                    "kind": "verify",
                    "command": "./tests/grok-route-test.sh",
                    "stage_id": "",
                }
            ],
            "verified": ["verify"],
        }
    )
    check(track.required_verify_steps() == ["verify"], "legacy verify stage keeps role identity")
    check(
        track.next_verify_step() is None and track.all_required_completed(),
        "legacy verified track remains complete",
    )


def test_consilium_failure_trigger(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "consilium-trigger"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {"role": "implement-hard", "required": True, "reason": "repair", "kind": "spawn"},
        ],
    )
    for _ in range(2):
        record_stage_tx(
            path,
            decision_id,
            "result",
            role="implement-hard",
            success=False,
            consilium_after_failures=3,
        )
    track = load_state(path).get_execution(decision_id)
    check(
        track.repair_failures == {"implement-hard": 2}
        and not any(stage.stage_id == "consilium" for stage in track.stages),
        "N-1 repair failures do not append consilium",
    )

    record_stage_tx(
        path,
        decision_id,
        "result",
        role="implement-hard",
        success=False,
        consilium_after_failures=3,
    )
    record_stage_tx(
        path,
        decision_id,
        "result",
        role="implement-hard",
        success=False,
        consilium_after_failures=3,
    )
    track = load_state(path).get_execution(decision_id)
    consilium = [stage for stage in track.stages if stage.stage_id == "consilium"]
    check(
        len(consilium) == 1 and len(consilium[0].members) == 3,
        "Nth and repeated repair failures append consilium exactly once",
    )
    round_trip = ExecutionTrack.from_dict(track.to_dict())
    check(
        round_trip.repair_failures == track.repair_failures
        and [stage.to_dict() for stage in round_trip.stages]
        == [stage.to_dict() for stage in track.stages],
        "consilium stage and repair counters round-trip",
    )

    record_stage_tx(
        path, decision_id, "result", role="implement-hard", success=True, consilium_after_failures=3
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track.repair_failures["implement-hard"] == 0, "successful repair resets its failure counter"
    )
    record_stage_tx(
        path,
        decision_id,
        "result",
        role="implement-hard",
        success=False,
        retrieval_task_id="retry",
        consilium_after_failures=3,
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track.repair_failures["implement-hard"] == 1,
        "repair failure after success restarts from zero",
    )


def test_consilium_barrier_gate_and_recipe(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "consilium-barrier"
    stage = ExecutionStage(
        "consilium",
        True,
        "stuck repair",
        kind="parallel_spawn",
        stage_id="consilium",
        members=(
            ExecutionMember("0", "consilium-analyst", True, "fresh-eyes telemetry analysis"),
            ExecutionMember("1", "consilium-challenger", True, "adversarial hypothesis cross-exam"),
            ExecutionMember("2", "consilium-arbiter", True, "verdict and repair plan"),
        ),
    )
    ensure_execution_tx(path, decision_id, SID, 1, [stage.to_dict()])
    route = {"intent": "implement", "profile": "default", "decision_id": decision_id}
    lifted, recipe = hook_route._enforceable_gate(decision_id, route)
    check(
        not lifted and "Consilium:" in recipe,
        "consilium barrier gates end_turn and recipe names Consilium",
    )
    for member in stage.members:
        key = ExecutionTrack.member_key("consilium", member.member_id)
        record_stage_tx(path, decision_id, "requested", role=key)
        record_stage_tx(path, decision_id, "result", role=key, success=True)
    track = load_state(path).get_execution(decision_id)
    lifted, _recipe = hook_route._enforceable_gate(decision_id, route)
    check(
        track.all_required_completed() and lifted,
        "three member results complete consilium and release end_turn",
    )


def test_foreground_spawn_result_metadata(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "foreground-spawn"
    ensure_execution_tx(
        path,
        decision_id,
        SID,
        1,
        [
            {"role": "review-hard", "required": True, "reason": "review", "kind": "spawn"},
        ],
    )
    record_stage_tx(path, decision_id, "requested", role="review-hard")
    task_id = "123e4567-e89b-12d3-a456-426614174000"
    original_route = hook_route.resolve_route
    hook_route.resolve_route = lambda *args, **kwargs: {
        "decision_id": decision_id,
        "mode": "dynamic",
        "profile": "default",
    }
    try:
        hook_route.handle_post_tool(
            {
                "toolName": "spawn_subagent",
                "sessionId": SID,
                "toolInput": {"subagent_type": "review-hard", "background": False},
                "toolResult": f"completed: quoted <subagent_meta>id={task_id}, type=review-hard</subagent_meta>\n"
                f"<subagent_meta>id={task_id}, type=review-hard</subagent_meta>",
            },
            load_intents(),
        )
    finally:
        hook_route.resolve_route = original_route
    track = load_state(path).get_execution(decision_id)
    check(
        track.completed == ["review-hard"] and not track.stage_tasks and not track.member_tasks,
        "foreground spawn metadata binds and records exactly once",
    )
    check(
        task_ids(
            {"toolResult": f"<subagent_meta>id={task_id}, type=review-hard</subagent_meta>"},
            include_result=True,
        )
        == [task_id],
        "foreground metadata parser accepts a final strict UUID line",
    )
    check(
        task_ids(
            {"toolResult": "<subagent_meta>id=bad, type=review-hard</subagent_meta>"},
            include_result=True,
        )
        == [],
        "foreground metadata parser rejects invalid UUIDs",
    )
    check(
        task_ids(
            {
                "toolResult": f"<subagent_meta>id={task_id}, type=review-hard</subagent_meta>\n"
                "<subagent_meta>id=bad, type=review-hard</subagent_meta>"
            },
            include_result=True,
        )
        == [],
        "foreground metadata parser uses only the final metadata line",
    )


def test_foreground_verify_fanout(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    command = "./tests/grok-route-test.sh"
    verify = {
        "role": "verify",
        "required": True,
        "reason": "verification",
        "kind": "verify",
        "command": command,
        "stage_id": "verify/0",
    }
    current_id = "foreground-verify-current"
    ensure_execution_tx(path, current_id, SID, 3, [verify])
    for debt_id in ("foreground-verify-a", "foreground-verify-b"):
        ensure_execution_tx(path, debt_id, SID, 1, [verify])
    record_verify_tx(path, current_id, "verify/0", True)
    count = record_verify_fanout_tx(path, SID, current_id, command, True)
    state = load_state(path)
    check(
        count == 2
        and all(
            state.get_execution(debt_id).verified == ["verify/0"]
            for debt_id in ("foreground-verify-a", "foreground-verify-b")
        ),
        "foreground verifier fans out to every matching debt track",
    )
    check(
        state.get_execution(current_id).verified == ["verify/0"],
        "foreground verifier fanout skips the current decision",
    )
    mismatch = record_verify_fanout_tx(
        path, SID, current_id, "./tests/grok-combine-install-test.sh", True
    )
    check(mismatch == 0, "foreground verifier fanout rejects command mismatches")
    pending_id = "foreground-verify-pending"
    ensure_execution_tx(
        path,
        pending_id,
        SID,
        1,
        [
            {"role": "review-hard", "required": True, "reason": "review", "kind": "spawn"},
            verify,
        ],
    )
    pending = record_verify_fanout_tx(path, SID, current_id, command, True)
    check(
        pending == 0 and not load_state(path).get_execution(pending_id).verified,
        "foreground verifier fanout skips tracks with unfinished spawn barriers",
    )


def test_foreground_verify_fanout_without_current_track(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    command = "./tests/grok-route-test.sh"
    verify = {
        "role": "verify",
        "required": True,
        "reason": "verification",
        "kind": "verify",
        "command": command,
        "stage_id": "verify/0",
    }
    current_id = "fanout-trackless-current"
    ensure_execution_tx(path, current_id, SID, 2, [])
    for debt_id in ("fanout-trackless-a", "fanout-trackless-b"):
        ensure_execution_tx(path, debt_id, SID, 1, [verify])

    def foreground(command_line: str, session: str) -> None:
        original_route = hook_route.resolve_route
        original_verifier = settlement.resolve_verifier
        hook_route.resolve_route = lambda *args, **kwargs: {
            "decision_id": current_id,
            "mode": "dynamic",
            "profile": "default",
        }
        settlement.resolve_verifier = lambda *args, **kwargs: (command,)
        try:
            hook_route.handle_post_tool(
                {
                    "toolName": "run_terminal_command",
                    "sessionId": session,
                    "toolInput": {"command": command_line},
                    "toolResult": {"exit_code": 0, "output": "verifier passed"},
                },
                load_intents(),
            )
        finally:
            hook_route.resolve_route = original_route
            settlement.resolve_verifier = original_verifier

    foreground(command, SID)
    state = load_state(path)
    check(
        all(
            state.get_execution(debt_id).verified == ["verify/0"]
            for debt_id in ("fanout-trackless-a", "fanout-trackless-b")
        ),
        "current turn without its own verify step still fans the workspace verifier to debt",
    )
    check(
        not state.get_execution(current_id).verified,
        "trackless current decision gains no verify step of its own",
    )

    ensure_execution_tx(path, "fanout-mismatch-debt", SID, 1, [verify])
    foreground("echo not a verifier", SID)
    check(
        not load_state(path).get_execution("fanout-mismatch-debt").verified,
        "foreground command outside the workspace verifier list records no debt",
    )

    subagent_sid = "01a0063b-a2dd-7bf2-aafd-4a66cf8377dd"
    ensure_execution_tx(path, "fanout-subagent-debt", subagent_sid, 1, [verify])
    plant_child_session(Path(os.environ["GROK_HOME"]), subagent_sid, [])
    foreground(command, subagent_sid)
    check(
        not load_state(path).get_execution("fanout-subagent-debt").verified,
        "subagent foreground verifier results never fan out to debt",
    )


def test_release_after_two_not_found(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "release-decision"
    stages = [
        {
            "stage_id": "recon",
            "role": "recon",
            "required": True,
            "kind": "parallel_spawn",
            "members": [
                {"member_id": "0", "role": "explore", "required": True, "reason": "test"},
            ],
        }
    ]
    ensure_execution_tx(path, decision_id, SID, 1, stages)
    key = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "task-a")
    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "task-a") == "streak",
        "first not_found keeps the binding",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.not_found_streaks[key]["count"] == 1,
        "first not_found records a one-entry streak",
    )
    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "old-task") == "unmatched",
        "stale task id is ignored",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.not_found_streaks[key]["count"] == 1,
        "stale task id does not change the streak",
    )
    check(
        release_unresolvable_binding_tx(path, SID, decision_id, "task-a") == "released",
        "second not_found releases the binding",
    )
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and key not in track.member_tasks and key in track.failed,
        "released member is failed and unbound",
    )
    check(
        track is not None
        and track.failure_reasons[key] == "task_not_found"
        and key not in track.requested
        and key not in track.requested_at,
        "release stores its reason and clears request evidence",
    )
    check(
        allocate_parallel_member_tx(path, decision_id, "explore") == key,
        "released member is retryable",
    )


def test_unresolvable_release_skips_consilium_counter(tmp: Path) -> None:
    setup_environment(tmp)
    path = default_state_path()
    decision_id = "release-counter-decision"
    ensure_execution_tx(
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
                "members": [
                    {"member_id": "0", "role": "explore", "required": True, "reason": "release"},
                    {"member_id": "1", "role": "explore", "required": True, "reason": "ordinary"},
                ],
            }
        ],
    )
    first = allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "release-a")
    release_unresolvable_binding_tx(path, SID, decision_id, "release-a")
    release_unresolvable_binding_tx(path, SID, decision_id, "release-a")
    second = allocate_parallel_member_tx(path, decision_id, "explore")
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None
        and first not in track.repair_failures
        and not any(stage.stage_id == "consilium" for stage in track.stages),
        "unresolvable release skips repair and consilium counters",
    )
    bind_parallel_member_task_tx(path, decision_id, "explore", "release-b")
    release_unresolvable_binding_tx(path, SID, decision_id, "release-b")
    release_unresolvable_binding_tx(path, SID, decision_id, "release-b")
    allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "ordinary-a")
    record_retrieval_result_tx(path, SID, decision_id, "ordinary-a", success=False)
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.repair_failures.get(second) == 1,
        "ordinary terminal failure increments repair counter once",
    )
    allocate_parallel_member_tx(path, decision_id, "explore")
    bind_parallel_member_task_tx(path, decision_id, "explore", "ordinary-b")
    record_retrieval_result_tx(path, SID, decision_id, "ordinary-b", success=False)
    track = load_state(path).get_execution(decision_id)
    check(
        track is not None and track.repair_failures.get(second) == 2,
        "ordinary second failure increments its own repair counter",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_verifier_metachar_controls_rejected()
        run_state_v8_golden()
        check(True, "state v8 golden serialization")
        test_ordered_multi_stage(tmp / "ordered")
        test_conductor_recon_diet(tmp / "recon-diet")
        test_conductor_recon_diet_security_control(tmp / "recon-diet-security-control")
        test_wrong_stage_denied(tmp / "wrong")
        test_failed_spawn_not_success(tmp / "failed")
        test_failed_retrieval_reopens_without_stop_reset(tmp / "failed-retrieval")
        test_pending_child_sweep_filters_completed_tracks_before_cap(tmp / "pending-child-cap")
        test_static_cache_no_io(tmp / "static")
        test_empty_submit_ignores_cached_route(tmp / "empty-submit")
        test_routing_config_unavailable_records(tmp / "config-unavailable")
        test_stale_neutral_signals(tmp / "stale")
        test_stage_validation(tmp / "validate")
        test_decisions_jsonl_round_trip_and_migration(tmp / "decision-history")
        test_save_state_migrates_legacy_history(tmp)
        test_decision_history_rotation(tmp / "decision-rotation")
        test_atomic_state_temp_gc(tmp / "state-temp-gc")
        test_concurrent_state_updates(tmp / "concurrent")
        test_security_no_implement_executor(tmp / "security-no-impl")
        test_risk_terms()
        test_same_model_terra_pipeline(tmp / "same-terra")
        test_verify_gate_and_results(tmp / "verify")
        test_circuit_fallback_reconciles_execution_track(tmp / "circuit-fallback")
        test_same_model_glm_security_pipeline(tmp / "same-glm")
        test_direct_review_pipeline(tmp / "review")
        test_explore_pipeline(tmp / "explore")
        test_plan_adr_pipeline(tmp / "plan")
        test_parallel_member_tracking(tmp / "parallel")
        test_pipeline_and_state_slot_identity()
        test_slot_reconciliation_contract(tmp / "slot-reconciliation")
        test_consilium_runtime_barrier(tmp / "consilium")
        test_terminal_task_state(tmp / "terminal-state")
        test_verify_background_ack_and_retrieval_tracking(tmp / "verify-background")
        test_released_verifier_tombstones_settle_success_and_failure(tmp / "verify-released")
        test_generic_retrieval_skips_verify_bindings(tmp / "generic-verify-skip")
        test_debt_verify_background_ack_and_retrieval(tmp / "verify-debt")
        test_multi_debt_verify_fanout(tmp / "verify-fanout")
        test_cross_turn_retrieval_failure_keeps_prior_track_terminal(tmp / "cross-turn-retrieval")
        test_subagent_retrieval_does_not_mutate_execution(tmp / "subagent-retrieval")
        test_retrieval_failure_telemetry_records_outcomes(tmp / "retrieval-telemetry")
        test_stop_child_failure_sweep(tmp / "child-sweep")
        test_cross_turn_session_debt(tmp / "cross-turn-debt")
        test_background_ack_binds_session_debt(tmp / "background-debt")
        test_background_ack_debt_binding_ambiguity(tmp / "background-ambiguity")
        test_debt_barrier_singleton_members_are_stamped(tmp / "debt-barrier-singleton")
        test_debt_task_reuse_precedes_track_filtering(tmp / "debt-task-reuse")
        test_debt_barrier_same_role_allocation(tmp / "debt-barrier-allocation")
        test_debt_same_role_cross_track_ambiguity(tmp / "debt-cross-track")
        test_debt_same_role_stage_mix_ambiguity(tmp / "debt-stage-mix")
        test_background_ack_current_track_first(tmp / "background-current-first")
        test_background_ack_debt_window_and_static_mode(tmp / "background-window-static")
        test_debt_window_expiry(tmp / "expired-debt")
        test_stop_counter_and_session_debt_state(tmp / "debt-state")
        test_incomplete_execution_prune_respects_debt_window(tmp / "prune-debt")
        test_consilium_failure_trigger(tmp / "consilium-trigger")
        test_consilium_barrier_gate_and_recipe(tmp / "consilium-barrier")
        test_legacy_verify_track()
        test_foreground_spawn_result_metadata(tmp / "foreground-spawn")
        test_foreground_verify_fanout(tmp / "foreground-verify")
        test_foreground_verify_fanout_without_current_track(tmp / "foreground-verify-trackless")
        test_release_after_two_not_found(tmp / "release-after-two-not-found")
        test_unresolvable_release_skips_consilium_counter(tmp / "release-counter")

    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("state machine tests passed")
    return 0


if __name__ == "__main__":
    run_standalone(main)


def test_verifier_comparison_matches_pipeline_parser() -> None:
    from grokbuild import pipeline

    cases = [
        ("python check.py", "python check.py", True),
        ("python 'check file.py'", "python check\\ file.py", True),
        ("$REPO_ROOT/scripts/check.sh", "$REPO_ROOT/scripts/check.sh", True),
        ("python check.py; rm -rf .", "python check.py; rm -rf .", False),
        ("python check.py", "python check.py; rm", False),
        ("python check.py", "", False),
        ("python 'check.py", "python 'check.py", False),
        ("python café.py", "python café.py", True),
    ]
    for command, expected, verdict in cases:
        assert pipeline.is_verifier_command(command, expected) is verdict
        assert is_verifier_command(command, expected) is verdict


PYTEST_ONLY = (
    "test_confirmation_batch_uses_generic_linear_stage_tracking",
    "test_record_decision_preserves_runtime_stages",
    "test_settlement_payload_heals_stale_conductor_pin",
    "test_blocked_child_classifier_defaults_and_defect_precedence",
    "test_blocked_review_defect_and_mixed_reports_hold_debt",
    "test_blocked_review_inconclusive_discharges_and_allows_repeated_stop",
    "test_ambiguous_blocked_writable_child_still_fails",
    "test_verifier_comparison_matches_pipeline_parser",
)
