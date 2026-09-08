#!/usr/bin/env python3
"""Hook tests for A+Stop: transcript fallback and Stop gate. No pytest."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import _Stdin, bootstrap, run_standalone

bootstrap()
ROOT = REPO_ROOT / "grokbuild"

import io
import json
import os
import tempfile
from contextlib import redirect_stdout


import grokbuild.hook as route  # noqa: E402
import grokbuild.settlement as settlement  # noqa: E402
import grokbuild.transcript as transcript  # noqa: E402
from grokbuild.classify import extract_user_text, load_intents, model_allowed
from grokbuild.features import extract_features  # noqa: E402
from grokbuild.router import route_prompt  # noqa: E402
from grokbuild.state import RuntimeState, save_state


def _fail(msg: str) -> None:
    print(f"FAIL {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"ok   {msg}")


def test_extract_user_query() -> None:
    wrapped = "<user_info>cwd=/tmp</user_info>\n<user_query>\nнайди RCE в demo-api\n</user_query>"
    if extract_user_text(wrapped) != "найди RCE в demo-api":
        _fail("extract_user_text should prefer <user_query>")
    if route_prompt(wrapped, spec=load_intents(), mode="static").intent != "security":
        _fail("classify should score only the user_query body")
    _ok("extract_user_query")


def test_model_catalog() -> None:
    if not model_allowed("glm-5.3", "glm-5.3"):
        _fail("glm-5.3 should match the canonical GLM binding")
    if model_allowed("glm-5.3-1m", "glm-5.3"):
        _fail("deleted GLM alias must not match the canonical binding")
    if model_allowed("gpt-5.6-terra", "glm-5.3"):
        _fail("terra is not glm")
    _ok("model_catalog")


def test_spawn_input_shapes() -> None:
    for payload in (
        {"toolInput": {"subagent_type": "explore"}},
        {
            "toolInput": {
                "tool_name": "spawn_subagent",
                "tool_input": {"subagent_type": "explore"},
            }
        },
    ):
        if settlement._spawn_subagent_type(payload) != "explore":
            _fail(f"spawn input shape was not extracted: {payload!r}")
    _ok("spawn input shapes")


def test_visual_tool_classification() -> None:
    if "request_visual_analysis" not in route.ALWAYS_READ:
        _fail("request_visual_analysis must be read-only")
    if "analyze_screenshot" in route.READ_TOOLS:
        _fail("unknown analyze_screenshot must not be read-only")
    _ok("visual tool classification")


def test_verifier_result_status_patterns() -> None:
    for text in ("0 failures", "no failures", "failed: 0", "0 errors"):
        if settlement._verify_result_status({"toolResult": text}) != "incomplete":
            _fail(f"ambiguous status should remain incomplete: {text}")
    for text in ("exit: 1\nvalidation rejected", "Exit Code: 2\nPermission denied"):
        if settlement._verify_result_status({"toolResult": text}) != "failure":
            _fail(f"nonzero textual exit should fail: {text}")
    if (
        settlement._verify_result_status({"toolResult": {"status": "failed", "output": "x"}})
        != "failure"
    ):
        _fail("structured failed status should fail")
    if settlement._verify_result_status({"toolResult": {"status": "passed"}}) != "success":
        _fail("structured passed status should succeed")
    if (
        settlement._verify_result_status({"toolResult": {"status": "failed", "exit_code": 0}})
        != "failure"
    ):
        _fail("structured failed status must override zero exit code")
    if (
        settlement._verify_result_status({"toolResult": {"status": "passed", "exit_code": 3}})
        != "success"
    ):
        _fail("structured passed status must override nonzero exit code")
    if settlement._verify_result_status({"toolResult": "all good"}) != "incomplete":
        _fail("unsupported prose should remain incomplete")
    for text in ("1 failed", "2 failures", "FAILED", "Traceback (most recent call last)"):
        if settlement._verify_result_status({"toolResult": text}) != "failure":
            _fail(f"explicit failure status should fail: {text}")
    if (
        settlement._verify_result_status({"toolResult": {"exit_code": 0, "output": "FAILED"}})
        != "success"
    ):
        _fail("structured exit code must override textual failure")
    _ok("verifier result status patterns")


def test_last_user_prompt(tmp: Path) -> None:
    history = tmp / "chat_history.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "content": "you are grok"}),
                json.dumps(
                    {
                        "type": "user",
                        "content": [{"type": "text", "text": "summarize this file"}],
                        "synthetic_reason": "compaction_meta",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "content": [
                            {"type": "image", "attachment_id": "opaque", "data": "ignored"},
                            {
                                "type": "text",
                                "text": "<user_query>\nнайди RCE в demo-api\n</user_query>",
                            },
                        ],
                    }
                ),
                json.dumps({"type": "assistant", "content": "looking"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    got = route.last_user_prompt("unused", history_path=history)
    if got != "найди RCE в demo-api":
        _fail(f"last_user_prompt={got!r}")
    _ok("last_user_prompt skips synthetic")


def test_skips_system_reminder(tmp: Path) -> None:
    history = tmp / "reminder.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "content": [
                            {"type": "text", "text": "<user_query>прошей роутер</user_query>"}
                        ],
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "content": "spawning",
                        "tool_calls": [
                            {
                                "name": "spawn_subagent",
                                "arguments": {"subagent_type": "implement"},
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<system-reminder>\nAvailable skills: security, "
                                    "implement. Use security for CVE work.\n</system-reminder>"
                                ),
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    got = route.last_user_prompt("unused", history_path=history)
    if got != "прошей роутер":
        _fail(f"system-reminder stole the prompt: {got!r}")
    if not route.turn_station_status("sid", "implement", history_path=history)["requested"]:
        _fail("system-reminder must not reset the spawn window")
    _ok("system-reminder is not a user turn")


def test_prompt_beyond_tail(tmp: Path) -> None:
    history = tmp / "huge.jsonl"
    with history.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "content": [
                        {"type": "text", "text": "<user_query>найди RCE в demo-api</user_query>"}
                    ],
                }
            )
            + "\n"
        )
        pad = json.dumps({"type": "tool_result", "content": "x" * 4000}) + "\n"
        while history.stat().st_size < route.TAIL_BYTES + 50_000:
            handle.write(pad)
            handle.flush()
    got = route.last_user_prompt("unused", history_path=history)
    if got != "найди RCE в demo-api":
        _fail(f"prompt beyond 256KB tail lost: {got!r}")
    _ok("prompt beyond 256KB tail")


def _run_main(payload: dict) -> tuple[int, str]:
    old = sys.stdin
    output = io.StringIO()
    try:
        sys.stdin = _Stdin(json.dumps(payload))
        with redirect_stdout(output):
            try:
                rc = route.main()
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        sys.stdin = old
    return rc, output.getvalue()


def _plant_hook_session(tmp: Path, prompt: str, model: str = "gpt-5.6-terra") -> str:
    os.environ["GROK_HOME"] = str(tmp / "grok")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ.pop("GROK_ROUTE_ENFORCE", None)
    session_id = "01a0063b-a2dd-7bf2-aafd-4a66cf8377cc"
    session = tmp / "grok" / "sessions" / "ws" / session_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": session_id}, "current_model_id": model}), encoding="utf-8"
    )
    (session / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": prompt}) + "\n", encoding="utf-8"
    )
    state = RuntimeState(source_path=tmp / "state" / "grok-route" / "state.json")
    state.set_available("security-verify", True, "verified test signal")
    state.set_available("review-independent", True, "verified test signal")
    save_state(state)
    _run_main({"hookEventName": "UserPromptSubmit", "sessionId": session_id, "prompt": prompt})
    return session_id


def _latest_record(tmp: Path, event: str) -> dict:
    log = tmp / "state" / "grok-route" / "route.jsonl"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return next(record for record in reversed(records) if record.get("event") == event)


def _check_pre_telemetry(tmp: Path, intent: str, reason_code: str) -> None:
    record = _latest_record(tmp, "pre_tool_use_outcome")
    if record.get("intent") != intent or record.get("gate_active") is not True:
        _fail(f"pre-tool telemetry route state mismatch: {record}")
    if record.get("reason_code") != reason_code:
        _fail(f"pre-tool telemetry reason mismatch: {record}")


def _check_stop_telemetry(
    tmp: Path, intent: str, reason_code: str, *, gate_active: bool = True
) -> None:
    record = _latest_record(tmp, "stop")
    if record.get("intent") != intent or record.get("gate_active") is not gate_active:
        _fail(f"Stop telemetry route state mismatch: {record}")
    if record.get("reason_code") != reason_code:
        _fail(f"Stop telemetry reason mismatch: {record}")


def _pre_tool(
    tmp: Path, prompt: str, tool: str, *, model: str = "gpt-5.6-terra", bypass_write: bool = False
) -> tuple[int, str]:
    session_id = _plant_hook_session(tmp, prompt, model)
    original_is_write = route._is_write_tool
    if bypass_write:
        route._is_write_tool = lambda *args, **kwargs: False
    try:
        return _run_main(
            {
                "hookEventName": "PreToolUse",
                "sessionId": session_id,
                "toolName": tool,
                "toolInput": {},
            }
        )
    finally:
        route._is_write_tool = original_is_write


def _stop(
    tmp: Path,
    prompt: str,
    *,
    model: str = "gpt-5.6-terra",
    reason: str = "end_turn",
    active: bool = False,
) -> tuple[int, str]:
    session_id = _plant_hook_session(tmp, prompt, model)
    return _run_main(
        {
            "hookEventName": "Stop",
            "sessionId": session_id,
            "reason": reason,
            "stopHookActive": active,
        }
    )


def test_soft_deny(tmp: Path) -> None:
    spec = load_intents()
    for prompt, expected in (("fix this", "implement"), ("hardening", "security")):
        features = extract_features(prompt, spec)
        decision = route_prompt(prompt, spec=spec, mode="static")
        if features.strong or not features.phrases or decision.intent != expected:
            _fail(f"phrase-only fixture is invalid: {prompt!r} {features} {decision.intent}")
    rc, out = _pre_tool(tmp / "strong", "найди RCE в demo-api", "search_replace")
    _check_pre_telemetry(tmp / "strong", "security", "zero_write")
    if rc != 2 or "zero-write" not in out:
        _fail(f"strong edit should soft-deny: {rc} {out!r}")
    _ok("integrated strong edit soft-deny")

    rc, out = _pre_tool(tmp / "read", "найди RCE в demo-api", "read_file")
    if rc != 0 or '"decision": "allow"' not in out:
        _fail(f"reads must pass: {rc} {out!r}")
    _ok("integrated read tool passes")

    rc, out = _pre_tool(tmp / "weak-implement", "fix this", "write")
    _check_pre_telemetry(tmp / "weak-implement", "implement", "zero_write")
    if rc == 0 or '"decision": "deny"' not in out:
        _fail(f"phrase-only implement must deny: {rc} {out!r}")
    _ok("integrated phrase-only implement denial")

    rc, out = _pre_tool(tmp / "weak-security", "hardening", "write", bypass_write=True)
    if rc != 0 or '"decision": "allow"' not in out:
        _fail(f"phrase-only security must pass: {rc} {out!r}")
    _ok("integrated phrase-only security pass")

    rc, out = _pre_tool(tmp / "same-model", "найди RCE в demo-api", "write", model="glm-5.3")
    _check_pre_telemetry(tmp / "same-model", "security", "zero_write")
    if rc == 0 or '"decision": "deny"' not in out:
        _fail(f"same-model conductor must still delegate: {rc} {out!r}")
    _ok("integrated same-model denial")

    for intent, prompt in (
        ("review", "review the diff"),
        ("plan", "write an ADR for adapters"),
        ("explore", "explore the codebase read-only"),
    ):
        rc, out = _pre_tool(tmp / intent, prompt, "write")
        _check_pre_telemetry(tmp / intent, intent, "zero_write")
        if rc == 0 or '"decision": "deny"' not in out:
            _fail(f"{intent} should be edit-gated: {rc} {out!r}")
    _ok("integrated review/plan/explore edit gating")

    os.environ["GROK_ROUTE_SOFT"] = "0"
    try:
        rc, out = _pre_tool(
            tmp / "soft-off", "найди RCE in demo-api", "search_replace", bypass_write=True
        )
    finally:
        os.environ.pop("GROK_ROUTE_SOFT", None)
    if rc != 0 or '"decision": "allow"' not in out:
        _fail(f"GROK_ROUTE_SOFT=0 must observe: {rc} {out!r}")
    _ok("integrated GROK_ROUTE_SOFT=0 observe mode")


def test_should_block_stop(tmp: Path) -> None:
    rc, out = _stop(tmp / "stop-strong", "найди RCE в demo-api")
    _check_stop_telemetry(tmp / "stop-strong", "security", "pending_stage")
    if '"decision": "block"' not in out or "subagent_type" not in out or "security" not in out:
        _fail(f"expected spawn recipe, got {rc} {out!r}")
    _ok("integrated strong security Stop block")

    rc, out = _stop(tmp / "stop-active", "найди RCE в demo-api", active=True)
    _check_stop_telemetry(tmp / "stop-active", "security", "pending_stage")
    if '"decision": "block"' not in out or "subagent_type" not in out or "security" not in out:
        _fail(f"stopHookActive is telemetry-only: {rc} {out!r}")
    _ok("integrated stopHookActive does not release")

    rc, out = _stop(tmp / "stop-shutdown", "найди RCE in demo-api", reason="shutdown")
    _check_stop_telemetry(tmp / "stop-shutdown", "security", "not_end_turn")
    if rc != 0 or '"decision": "block"' in out:
        _fail(f"shutdown should pass: {rc} {out!r}")
    _ok("integrated non-end_turn pass")

    rc, out = _stop(tmp / "stop-weak-security", "hardening")
    _check_stop_telemetry(tmp / "stop-weak-security", "security", "gate_lifted", gate_active=False)
    if '"decision": "block"' in out:
        _fail(f"phrase-only security should not Stop: {rc} {out!r}")
    _ok("integrated weak security does not Stop")

    rc, out = _stop(tmp / "stop-weak-implement", "fix this")
    _check_stop_telemetry(tmp / "stop-weak-implement", "implement", "pending_stage")
    if '"decision": "block"' not in out:
        _fail(f"phrase-only implement should Stop: {rc} {out!r}")
    _ok("integrated weak implement Stop block")

    rc, out = _stop(tmp / "stop-same-model", "найди RCE в demo-api", model="glm-5.3")
    _check_stop_telemetry(tmp / "stop-same-model", "security", "pending_stage")
    if '"decision": "block"' not in out:
        _fail(f"same-model conductor must still delegate: {rc} {out!r}")
    _ok("integrated same-model Stop block")

    for intent, prompt in (
        ("review", "review the diff"),
        ("plan", "write an ADR for adapters"),
        ("explore", "explore the codebase read-only"),
    ):
        rc, out = _stop(tmp / f"stop-{intent}", prompt)
        _check_stop_telemetry(tmp / f"stop-{intent}", intent, "pending_stage")
        if '"decision": "block"' not in out:
            _fail(f"{intent} should be Stop-gated: {rc} {out!r}")
    _ok("integrated review/plan/explore Stop gating")


def test_feedback_filter(tmp: Path) -> None:
    history = tmp / "feedback.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "content": "route=review: review the diff",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "content": (route.station_recipe({"intent": "review"})),
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "content": route.station_recipe({"intent": "explore"}),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    got = route.last_user_prompt("unused", history_path=history)
    if got != "route=review: review the diff":
        _fail(f"legitimate route=review override was dropped: {got!r}")
    _ok("feedback filter is narrow")


def test_last_child_assistant_skips_synthetic_records(tmp: Path) -> None:
    history = tmp / "chat_history.jsonl"
    history.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "user", "content": "run the child"},
                {"type": "assistant", "content": "BLOCKED: real failure"},
                {
                    "type": "assistant",
                    "content": "synthetic summary",
                    "synthetic_reason": "compaction_meta",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    original = settlement._session_dir
    settlement._session_dir = lambda _task_id: tmp
    try:
        detected = settlement._last_child_assistant_text("child")
    finally:
        settlement._session_dir = original
    if detected != "BLOCKED: real failure":
        _fail("synthetic assistant must not mask a real BLOCKED report")

    history.write_text(
        json.dumps(
            {
                "type": "assistant",
                "content": "synthetic summary",
                "synthetic_reason": "compaction_meta",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original = settlement._session_dir
    settlement._session_dir = lambda _task_id: tmp
    try:
        detected = settlement._last_child_assistant_text("child")
    finally:
        settlement._session_dir = original
    if detected:
        _fail("synthetic-only child transcript must not produce a report")
    _ok("last_child_assistant_skips_synthetic_records")


def test_session_id_allowlist(tmp: Path) -> None:
    os.environ["GROK_HOME"] = str(tmp / "grok")
    planted = tmp / "grok" / "planted"
    planted.mkdir(parents=True)
    (planted / "summary.json").write_text("{}", encoding="utf-8")
    if route._session_dir("../../planted") is not None:
        _fail("path traversal session id must be rejected")
    if route._session_dir("*") is not None:
        _fail("glob session id must be rejected")
    if route._session_dir("sid-1") is not None:
        _fail("non-uuid session id must be rejected")
    _ok("session id allowlist")


def test_hook_cli(tmp: Path) -> None:
    os.environ["GROK_HOME"] = str(tmp / "grok")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ.pop("GROK_ROUTE_ENFORCE", None)

    session_id = "01a0063b-a2dd-7bf2-aafd-4a66cf8377cc"
    session = tmp / "grok" / "sessions" / "ws" / session_id
    session.mkdir(parents=True)
    (session / "summary.json").write_text(
        json.dumps({"info": {"id": session_id}, "current_model_id": "gpt-5.6-terra"}),
        encoding="utf-8",
    )
    (session / "chat_history.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "content": [{"type": "text", "text": "<user_query>прошей роутер</user_query>"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import io
    from contextlib import redirect_stdout

    old_stdin = sys.stdin
    buf = io.StringIO()
    rc = 0
    try:
        sys.stdin = _Stdin(
            json.dumps(
                {
                    "hookEventName": "PreToolUse",
                    "sessionId": session_id,
                    "toolName": "search_replace",
                }
            )
        )
        with redirect_stdout(buf):
            try:
                rc = route.main()
            except SystemExit as exc:
                rc = int(exc.code or 0)
    finally:
        sys.stdin = old_stdin
    out = buf.getvalue()
    if rc != 2 or '"decision": "deny"' not in out:
        _fail(f"strong implement edit should soft-deny: rc={rc} out={out!r}")
    saved = json.loads((tmp / "state" / "grok-route" / "state.json").read_text(encoding="utf-8"))
    executions = saved.get("executions") or {}
    if not any(
        (s.get("role") or "").startswith("implement")
        for t in executions.values()
        for s in (t.get("stages") or [])
    ):
        _fail(f"pre_tool did not record the execution track: {saved}")
    _ok("pre_tool records execution track")

    old_stdin = sys.stdin
    captured: list[str] = []

    class _Capture(_Stdin):
        def __init__(self, text: str) -> None:
            super().__init__(text)

    try:
        sys.stdin = _Capture(
            json.dumps(
                {
                    "hookEventName": "Stop",
                    "sessionId": session_id,
                    "reason": "end_turn",
                    "stopHookActive": False,
                }
            )
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = route.main()
        captured.append(buf.getvalue())
    finally:
        sys.stdin = old_stdin
    if rc != 0:
        _fail(f"stop main rc={rc}")
    out = captured[0]
    if '"decision": "block"' not in out or "subagent_type=" not in out or "implement" not in out:
        _fail(f"stop should block with spawn recipe: {out!r}")
    _ok("stop transcript fallback blocks")

    buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = _Stdin(
            json.dumps(
                {
                    "hookEventName": "UserPromptSubmit",
                    "sessionId": session_id,
                    "prompt": "найди RCE в demo-api",
                }
            )
        )
        with redirect_stdout(buf):
            rc = route.main()
    finally:
        sys.stdin = old_stdin
    out = buf.getvalue()
    if rc != 0 or out.strip():
        _fail(f"submit is passive and must emit nothing: rc={rc} out={out!r}")
    _ok("submit is passive (no additionalContext)")


def test_append_log_skips_state_load_when_availability_present(tmp: Path) -> None:
    loads = 0
    original_load = route.load_state
    original_append = route.append_jsonl

    def counted_load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return original_load(*args, **kwargs)

    route.load_state = counted_load
    route.append_jsonl = lambda path, payload: None
    try:
        route._append_log({"provider_availability": {"provider": {"available": True}}})
    finally:
        route.load_state = original_load
        route.append_jsonl = original_append
    if loads != 0:
        _fail(f"_append_log eagerly loaded state {loads} times")
    _ok("append_log_present_availability_skips_state_load")


def test_turn_count_cache(tmp: Path) -> None:
    history = tmp / "turn-cache.jsonl"
    records = [
        {"type": "user", "content": "first"},
        {"type": "assistant", "content": "reply"},
        {"type": "user", "content": "synthetic", "synthetic_reason": "feedback"},
        {"type": "user", "content": "second"},
    ]
    history.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    expected = sum(route._is_real_user(item) for item in records)
    scans = 0
    original_iter = transcript._iter_jsonl

    def counted_iter(path):
        nonlocal scans
        scans += 1
        yield from original_iter(path)

    transcript._TURN_COUNT_CACHE.clear()
    transcript._iter_jsonl = counted_iter
    try:
        first = route.last_user_turn_number(None, history)
        second = route.last_user_turn_number(None, history)
        history.write_text(
            history.read_text(encoding="utf-8")
            + json.dumps({"type": "user", "content": "third"})
            + "\n",
            encoding="utf-8",
        )
        third = route.last_user_turn_number(None, history)
    finally:
        transcript._iter_jsonl = original_iter
        transcript._TURN_COUNT_CACHE.clear()
    if first != expected or second != expected or third != expected + 1 or scans != 2:
        _fail(f"turn cache mismatch: counts={(first, second, third)} scans={scans}")
    _ok("turn_count_cache_identity_and_invalidation")


def main() -> int:
    spec = load_intents()
    if spec.get("enforce_default"):
        _fail("enforce_default must stay false")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_extract_user_query()
        test_model_catalog()
        test_visual_tool_classification()
        test_verifier_result_status_patterns()
        test_last_user_prompt(tmp)
        test_skips_system_reminder(tmp)
        test_prompt_beyond_tail(tmp)
        test_soft_deny(tmp)
        test_should_block_stop(tmp)
        test_feedback_filter(tmp)
        test_last_child_assistant_skips_synthetic_records(tmp)
        test_session_id_allowlist(tmp)
        test_append_log_skips_state_load_when_availability_present(tmp)
        test_turn_count_cache(tmp)
        test_hook_cli(tmp)
    print("hook tests passed")
    return 0


if __name__ == "__main__":
    run_standalone(main)


PYTEST_ONLY = ("test_spawn_input_shapes",)
