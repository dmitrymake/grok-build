#!/usr/bin/env python3
"""Observe and enforce Grok Build model routing.

UserPromptSubmit passively classifies and persists the route and emits nothing.
PreToolUse emits exactly one terminal allow or deny outcome: tight read tools
pass, while edit tools and write shell commands are denied because the
conductor is permanently zero-write; exact configured verifiers are allowed
when due. The hook denies (fail-closed) when the gate is active; only
identified implementation children may write. Stop blocks a genuine end_turn while required work or session debt
remains, up to the profile limit. If UserPromptSubmit is mute, PreToolUse/Stop
classify the last real user turn from chat_history.jsonl.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime

from grokbuild.classify import (  # noqa: E402,F401
    STOP_FEEDBACK_MARK,
    load_intents,
)
from grokbuild.cache import cache_get, cache_put  # noqa: E402
from grokbuild.decision import decision_hook_record, hash_id, SCHEMA_VERSION, TELEMETRY_GENERATION
from grokbuild.endpoint_resolution import (  # noqa: E402
    make_endpoint_resolution,
    persist_endpoint_resolution,
)
import grokbuild.roles as roles
import grokbuild.transcript as transcript
from grokbuild.pipeline import Pipeline  # noqa: E402
from grokbuild.availability import _model_endpoint_availability  # noqa: E402
from grokbuild.verifiers import apply_config_verifiers, resolve_verifier  # noqa: E402
from grokbuild.redact import redact  # noqa: E402
from grokbuild.remediation import (  # noqa: E402
    open_remediation_debt,
    open_remediation_debts,
    record_remediation_stop,
    remediation_stop_context,
)
from grokbuild.safe_alternatives import classify_operation, safe_alternative  # noqa: E402
from grokbuild.payloads import (  # noqa: E402
    dump_payload_debug,
    says_subagent,
    task_ids,
)
from grokbuild.state import default_log_path, default_state_path, load_state
from grokbuild.transactions import (
    allocate_parallel_member_tx,
    ensure_turn,
    record_stage_tx,
    record_stop_block_tx,
)
from grokbuild.persist import append_jsonl
from grokbuild.policy import is_truthy_env, load_profiles  # noqa: E402
from grokbuild.transactions import (  # noqa: F401
    bind_debt_stage_task_tx,
    bind_debt_verify_task_tx,
    bind_linear_stage_task_tx,
    bind_parallel_member_task_tx,
    bind_terminal_task_tx,
    bind_verify_task_tx,
    record_retrieval_result_tx,
    record_verify_fanout_tx,
    record_verify_retrieval_tx,
    record_verify_tx,
)

from grokbuild.shell_guard import (  # noqa: F401
    ALWAYS_READ,
    CONDUCTOR_RECON_TOOLS,
    READ_TOOLS,
    _is_write_tool,
    _tool_command,
    readonly_shell_verdict,
    SCHEDULER_WRITE_TOOLS,
    is_readonly_shell,
    is_recon_shell,
)
from grokbuild.transcript import (  # noqa: F401
    SPAWN_TOOLS,
    STATION_SKILLS,
    TAIL_BYTES,
    _canonical_role,
    _current_model,
    _is_subagent,
    _is_real_user,
    _iter_jsonl_reversed,
    _message_text,
    _session_dir,
    _walk_prompt,
    last_user_prompt,
    last_user_prompt_raw,
    last_user_turn_number,
    turn_station_status,
)
from grokbuild.gate import (  # noqa: F401
    _barrier_recipe,
    _circuit_params,
    _consilium_threshold,
    _ensure_track,
    _is_recon_stage,
    _declarative_gated,
    _role_signal_unverified,
    EDIT_TOOLS,
    _enforceable_gate,
    _gate_active,
    _hold_recipe,
    _is_verify_command,
    _load_execution,
    _next_required_spawn_role,
    _next_required_spawn_stage,
    _pipeline_unfinished,
    _stage_recipe,
    _stop_params,
    _verify_command_for,
    _verify_recipe,
    station_recipe,
)
from grokbuild.settlement import handle_post_tool, handle_post_tool_failure
from grokbuild.settlement import (
    _attach_stage_spec,
    _handle_failure,
    _spawn_subagent_type,
    _sweep_pending_children,
)

# Grok records spawn requests as `spawn_subagent`; the legacy `Task` name is
# kept so the hook never misses an older-format spawn call.
READ_ONLY_ROLES = roles.EXECUTION_READ_ONLY_ROLES


def _now() -> str:
    return datetime.now(UTC).isoformat()


_STDIN_UNPARSEABLE = False


def _read_stdin() -> dict:
    global _STDIN_UNPARSEABLE
    _STDIN_UNPARSEABLE = False
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            _STDIN_UNPARSEABLE = True
            return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _STDIN_UNPARSEABLE = True
        return {}
    if not isinstance(data, dict):
        _STDIN_UNPARSEABLE = True
        return {}
    return data


def _append_log(payload: dict, state: object | None = None) -> None:
    # Transactional append + bounded rotation shared with the pipeline.
    payload.setdefault("second_opinion", None)
    if "provider_availability" not in payload:
        runtime_state = state if state is not None else load_state(default_state_path())
        payload["provider_availability"] = runtime_state.provider_availability_snapshot()
    append_jsonl(default_log_path(), payload)


def _dump_payload_debug(data: dict) -> None:
    dump_payload_debug(data, default_log_path().parent, _now())


def _event_name(data: dict) -> str:
    return (
        str(data.get("hookEventName") or os.environ.get("GROK_HOOK_EVENT") or "")
        .replace("-", "_")
        .casefold()
    )


def _session_id(data: dict) -> str | None:
    return (
        data.get("sessionId") or data.get("session_id") or os.environ.get("GROK_SESSION_ID") or None
    )


def _load_last_route(session_id: str | None) -> dict:
    cached = cache_get(session_id)
    return cached or {}


def _enforce_enabled(spec: dict) -> bool:
    flag = os.environ.get("GROK_ROUTE_ENFORCE")
    if flag is None:
        return bool(spec.get("enforce_default"))
    return is_truthy_env(os.environ, "GROK_ROUTE_ENFORCE")


def _allow(updated_input: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"decision": "allow"}
    if updated_input is not None:
        payload["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "updatedInput": updated_input,
        }
    print(json.dumps(payload))


def _deny(reason: str, safe_hint: dict[str, str] | None = None) -> None:
    """Emit actionable lifecycle coordination feedback, not a crash-like denial."""
    message = f"Stage Gating: this is normal lifecycle coordination, not a hook failure. {reason}"
    payload: dict[str, object] = {"decision": "deny", "reason": message}
    if safe_hint is not None:
        payload["safe_alternative"] = safe_hint
    print(json.dumps(redact(payload), ensure_ascii=False))
    raise SystemExit(2)


def _block_stop(reason: str) -> None:
    print(json.dumps(redact({"decision": "block", "reason": reason}), ensure_ascii=False))


def resolve_route(
    session_id: str | None,
    spec: dict,
    hook_prompt: str = "",
    persist: bool = False,
    event: str = "resolve",
    extra: dict | None = None,
    workspace_root: str | None = None,
) -> dict:
    """Classify the live user turn. Transcript wins over a stale cache.

    The pipeline is fed the *raw* trusted-user text (unstripped), so scoring
    can preserve a strong intent signal hidden inside a fake mid-prompt
    ``<system-reminder>``/``<user_info>`` block. Synthetic/feedback/reminder
    turns are still filtered by ``_is_real_user`` before we get here.
    """
    static_mode = (os.environ.get("GROK_ROUTE_MODE") or "").strip().casefold() == "static"
    # Transcript text has already crossed the host provenance boundary: use the
    # harness-stripped user view so appended reminders cannot score. The submit
    # payload remains the user-authored view and retains fake-tag resilience.
    transcript_raw = last_user_prompt(session_id)
    hook_raw = hook_prompt or ""
    # Submit often fires before chat_history has this turn; later events
    # prefer the transcript so a mute UserPromptSubmit still classifies.
    if event in {"user_prompt_submit"}:
        raw_prompt = hook_raw or transcript_raw
        source = "hook" if hook_raw else ("transcript" if transcript_raw else "none")
    else:
        raw_prompt = transcript_raw or hook_raw
        source = "transcript" if transcript_raw else ("hook" if hook_raw else "none")
    if not raw_prompt:
        cached = {} if static_mode else _load_last_route(session_id)
        if (
            not persist
            and event != "user_prompt_submit"
            and (cached.get("intent") or cached.get("has_strong"))
        ):
            record = dict(cached)
            record["observed_at"] = _now()
            record["event"] = event
            record["source"] = "cache"
            # A stale cached route can never gate: observe-only.
            record["mode"] = "shadow"
            cached_id = record.get("decision_id")
            record["decision_id"] = (
                cached_id
                if isinstance(cached_id, str) and cached_id
                else hash_id("stale-cache", session_id or "unknown")
            )
            record["execution"] = []
            record["write_policy"] = "observe"
            warnings = list(record.get("warnings") or [])
            warnings.append("stale_cache_observe")
            record["warnings"] = warnings
            if extra:
                record.update(extra)
            return record
        pipeline = Pipeline(spec=spec, enforce_default=bool(spec.get("enforce_default")))
        decision = pipeline.run(
            "",
            session_id=session_id,
            current_model=_current_model(session_id),
            event=event,
            source=source,
            persist=persist,
            workspace_root=workspace_root,
        )
        record = decision_hook_record(decision)
        record["provider_availability"] = pipeline.state.provider_availability_snapshot()
        record["_executable_bindings"] = pipeline.executable_bindings
        if event == "pre_tool_use":
            record["enforce"] = _enforce_enabled(spec)
        if extra:
            record.update(extra)
        record["_runtime_state"] = pipeline.state
        return record

    current = _current_model(session_id)
    pipeline = Pipeline(
        spec=spec,
        enforce_default=bool(spec.get("enforce_default")),
    )
    turn_id = None
    if event not in {"user_prompt_submit"}:
        turn_id = last_user_turn_number(session_id)
    decision = pipeline.run(
        raw_prompt,
        session_id=session_id,
        current_model=current,
        event=event,
        source=source,
        persist=persist,
        workspace_root=workspace_root,
        turn_id=turn_id,
    )
    if turn_id is not None and (decision.mode or "static") != "static":
        ensure_turn(default_state_path(), session_id, turn_id)
    record = decision_hook_record(decision)
    record["provider_availability"] = pipeline.state.provider_availability_snapshot()
    record["_executable_bindings"] = pipeline.executable_bindings
    if event == "pre_tool_use":
        record["enforce"] = _enforce_enabled(spec)
    if extra:
        record.update(extra)
    if persist and source != "none" and (record.get("mode") or "dynamic") != "static":
        cache_put(record)
    record["_runtime_state"] = pipeline.state
    return record


def handle_prompt(data: dict, spec: dict) -> None:
    session_id = _session_id(data)
    prompt = _walk_prompt(data) or ""
    # UserPromptSubmit is passive: stdout (including additionalContext) is
    # ignored by Grok, so we only classify/persist here and never emit output.
    # Pass the raw prompt so the scorer sees fake mid-prompt reminder/info
    # blocks (resolve_route strips for display/recipes but scores raw too).
    resolve_route(
        session_id,
        spec,
        hook_prompt=prompt,
        persist=True,
        event="user_prompt_submit",
        extra={"cwd": data.get("cwd") or data.get("workspaceRoot")},
        workspace_root=data.get("workspaceRoot") or data.get("cwd"),
    )


def _spawn_updated_input(
    data: dict, binding: Mapping[str, object] | None, *, force_capability: bool = False
) -> dict[str, object] | None:
    if not binding and not force_capability:
        return None
    tool_input = data.get("toolInput") or data.get("tool_input") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return None
    if not isinstance(tool_input, dict):
        return None
    updated = dict(tool_input)
    nested = tool_input.get("tool_input")
    if isinstance(nested, dict):
        updated_nested = dict(nested)
        if binding and binding.get("model"):
            updated_nested["model"] = str(binding["model"])
        if force_capability:
            updated_nested["capability_mode"] = "all"
        updated["tool_input"] = updated_nested
    else:
        if binding and binding.get("model"):
            updated["model"] = str(binding["model"])
        if force_capability:
            updated["capability_mode"] = "all"
    return updated


def _fresh_spawn_binding(
    route: Mapping[str, object], requested: str, session_id: str | None
) -> tuple[Mapping[str, object] | None, dict[str, object] | None]:
    registry = roles.load_registry()
    _, _, event = _model_endpoint_availability(
        route.get("_runtime_state"), requested, registry, session_id
    )
    if event is None:
        return None, None
    record = make_endpoint_resolution(
        decision_id=str(route.get("decision_id") or ""),
        session_id=session_id,
        event=event,
    )
    persist_endpoint_resolution(record)
    telemetry = record.to_dict()
    binding = event.get("executable_binding")
    if not isinstance(binding, Mapping) or binding.get("model") == event.get("model"):
        return None, telemetry
    return binding, telemetry


def _finish_pre_tool(
    outcome: str,
    reason_code: str,
    *,
    state: object | None = None,
    operation_class: str | None = None,
    updated_input: dict[str, object] | None = None,
    **fields: object,
) -> None:
    """Write exactly one terminal PreToolUse outcome before allowing/denying."""
    detail = str(fields.pop("detail", ""))
    payload = {
        "version": SCHEMA_VERSION,
        "telemetry_generation": TELEMETRY_GENERATION,
        "observed_at": fields.pop("observed_at", _now()),
        "event": "pre_tool_use_outcome",
        "outcome": outcome,
        "reason_code": reason_code,
        **({"operation_class": operation_class} if operation_class else {}),
        **fields,
    }
    safe_hint = None
    try:
        if outcome == "deny":
            safe_hint = redact(safe_alternative(reason_code, operation_class=operation_class))
            if safe_hint is not None:
                payload["safe_alternative"] = safe_hint
    except Exception:
        safe_hint = None
    try:
        _append_log(payload, state=state)
        if outcome == "deny" and safe_hint is not None and payload.get("gate_active") is True:
            open_remediation_debt(
                session_id=str(payload.get("session_id") or "") or None,
                decision_id=str(payload.get("decision_id") or "") or None,
                reason_code=operation_class or reason_code,
                current_branch=str(payload.get("next_stage") or safe_hint["kind"]),
                safe_alternative=safe_hint,
                debt_window_seconds=float(payload.get("debt_window_seconds", 86400.0)),
            )
    except Exception:
        pass
    if outcome == "deny":
        _deny(detail, safe_hint)
    _allow(updated_input)


def handle_pre_tool(data: dict, spec: dict) -> None:
    session_id = _session_id(data)
    tool = str(data.get("toolName") or data.get("tool_name") or "").casefold()
    payload_sub = says_subagent(data)
    if payload_sub:
        transcript.ensure_subagent_pin(session_id)
    if _is_subagent(session_id) or payload_sub:
        _finish_pre_tool(
            "allow",
            "subagent_exemption",
            session_id=session_id,
            tool=tool,
            decision_id=f"subagent:{session_id or 'unknown'}",
            turn_id=None,
            intent=None,
            role=None,
            model=None,
            effort=None,
            mode=None,
            gate_active=False,
        )
        return
    route = resolve_route(
        session_id,
        spec,
        persist=False,
        event="pre_tool_use",
        extra={"tool": tool},
        workspace_root=data.get("workspaceRoot") or data.get("cwd"),
    )
    mode = route.get("mode") or "dynamic"
    decision_id = route.get("decision_id")
    gate_active = _gate_active(route, spec)
    common = {
        "observed_at": route.get("observed_at") or _now(),
        "decision_id": decision_id,
        "turn_id": route.get("turn_id"),
        "session_id": session_id,
        "tool": tool,
        "intent": route.get("intent"),
        "role": route.get("role"),
        "model": route.get("required_model"),
        "effort": route.get("required_effort"),
        "mode": mode,
        "gate_active": gate_active,
        "second_opinion": route.get("second_opinion"),
        "provider_availability": route.get("provider_availability", {}),
        "debt_window_seconds": _route_debt_window(route),
    }

    def finish(outcome: str, code: str, detail: str = "", **fields: object) -> None:
        _finish_pre_tool(
            outcome, code, state=route.get("_runtime_state"), detail=detail, **common, **fields
        )

    track = _load_execution(decision_id) if gate_active else None
    next_stage = _next_required_spawn_stage(track) if track else None
    recon_tool = tool in CONDUCTOR_RECON_TOOLS or tool == "monitor" or (
        tool == "run_terminal_command" and is_recon_shell(_tool_command(data))
    )

    if tool in SCHEDULER_WRITE_TOOLS:
        finish(
            "deny",
            "zero_write",
            "Scheduler writes are deferred external execution; only a writable child may perform them.",
        )
        return
    if tool == "monitor":
        if gate_active and _is_recon_stage(track, next_stage):
            detail = (
                _barrier_recipe(route, track, next_stage)
                if next_stage.kind == "parallel_spawn"
                else _stage_recipe(route, next_stage.role, track)
            )
            finish("deny", "recon_diet", detail, next_stage=next_stage.stage_id or next_stage.role)
            return
        verdict = readonly_shell_verdict(_tool_command(data))
        if not data.get("toolInputTruncated") and verdict == "readonly":
            finish("allow", "read_allowed")
        else:
            code = "zero_write" if verdict == "write" else "shell_not_readonly"
            finish(
                "deny",
                code,
                "The command could not be proven read-only; deferred external execution must use a writable child.",
            )
        return

    if gate_active and decision_id:
        _ensure_track(route, decision_id)

    workspace_verifiers = resolve_verifier(data.get("workspaceRoot") or data.get("cwd"), spec)
    if tool == "run_terminal_command":
        track = _load_execution(decision_id) if decision_id else None
        next_verify = track.next_verify_step() if track else None
        if (
            next_verify
            and _next_required_spawn_role(track) is None
            and _is_verify_command(_tool_command(data), _verify_command_for(track, next_verify))
        ):
            finish("allow", "exact_verifier_allowed", next_stage=next_verify)
            return
        if (
            not gate_active
            and workspace_verifiers
            and any(
                _is_verify_command(_tool_command(data), command) for command in workspace_verifiers
            )
        ):
            finish("allow", "workspace_verifier_allowed")
            return

    if tool in SPAWN_TOOLS:
        if not gate_active:
            # An ungated spawn is still a spawn: a role whose model declares
            # several endpoints gets the same rotation, quota and circuit-aware
            # binding the gated path applies, otherwise every ad-hoc consilium
            # or review after the pipeline finishes lands on the default
            # endpoint and a held provider is hit again. Routing is untouched.
            updated_input = None
            endpoint_resolution = None
            requested = _canonical_role(_spawn_subagent_type(data))
            if mode != "static" and requested:
                binding, endpoint_resolution = _fresh_spawn_binding(route, requested, session_id)
                updated_input = _spawn_updated_input(data, binding)
            finish(
                "allow",
                "gate_inactive",
                endpoint_resolution=endpoint_resolution,
                updated_input=updated_input,
            )
            return
        track = _load_execution(decision_id)
        lifted, lifted_detail = _enforceable_gate(decision_id, route)
        # A stalled barrier is warning telemetry, not a bypass: continue through
        # normal member, binding, capability, and request bookkeeping validation.
        next_stage = _next_required_spawn_stage(track) if track else None
        if next_stage is None:
            lifted, detail = _enforceable_gate(decision_id, route)
            if lifted:
                finish("allow", "gate_lifted")
            else:
                finish(
                    "deny",
                    "pending_verify",
                    detail,
                    next_stage=track.next_verify_step() if track else "verify",
                )
            return
        if next_stage.kind == "sentinel":
            # The sentinel role cannot be spawned, so a stage recipe naming it is
            # unfollowable guidance. Deny with the sentinel's own reason, which is
            # exactly what the Stop gate reports for the same track.
            finish("deny", "blocked_sentinel", next_stage.reason, next_stage=next_stage.stage_id)
            return
        requested = _canonical_role(_spawn_subagent_type(data))
        registry = roles.load_registry()
        requested_role = registry.get(requested)
        force_capability = bool(requested_role is not None and requested_role.write)
        if next_stage.kind == "parallel_spawn":
            key = allocate_parallel_member_tx(
                default_state_path(), decision_id, requested, next_stage.stage_id
            )
            if key is None:
                finish(
                    "deny",
                    "wrong_parallel_member",
                    _barrier_recipe(route, track, next_stage),
                    next_stage=next_stage.stage_id,
                    member=requested,
                )
                return
            binding, endpoint_resolution = _fresh_spawn_binding(route, requested, session_id)
            updated_input = _spawn_updated_input(data, binding, force_capability=force_capability)
            request_ids = task_ids(data)
            _attach_stage_spec(
                route=route,
                track=track,
                decision_id=decision_id,
                session_id=session_id,
                stage_key=key,
                role=requested,
            )
            record_stage_tx(
                default_state_path(),
                decision_id,
                "requested",
                role=key,
                task_id=request_ids[0] if request_ids else None,
            )
            finish(
                "allow",
                "parallel_member_allowed",
                next_stage=next_stage.stage_id,
                member=key,
                endpoint_resolution=endpoint_resolution,
                updated_input=updated_input,
            )
            return
        if requested != next_stage.role:
            detail = _stage_recipe(route, next_stage.role, track)
            stage_was_spawned = bool(
                track
                and (
                    next_stage.role in track.requested
                    or next_stage.role in track.requested_at
                    or next_stage.role in track.stage_tasks
                )
            )
            if stage_was_spawned:
                detail += (
                    " A completed background stage whose retrieval did not register can be closed "
                    "by re-spawning the role with background: false."
                )
            finish("deny", "wrong_stage", detail, next_stage=next_stage.role)
            return
        binding, endpoint_resolution = _fresh_spawn_binding(route, requested, session_id)
        updated_input = _spawn_updated_input(data, binding, force_capability=force_capability)
        _attach_stage_spec(
            route=route,
            track=track,
            decision_id=decision_id,
            session_id=session_id,
            stage_key=next_stage.role,
            role=next_stage.role,
        )
        record_stage_tx(default_state_path(), decision_id, "requested", role=next_stage.role)
        finish(
            "allow",
            "spawn_allowed",
            next_stage=next_stage.role,
            endpoint_resolution=endpoint_resolution,
            updated_input=updated_input,
        )
        return

    if _is_write_tool(tool, data):
        if tool == "run_terminal_command" and gate_active:
            track = _load_execution(decision_id)
            next_verify = track.next_verify_step() if track else None
            if (
                next_verify
                and _next_required_spawn_role(track) is None
                and _is_verify_command(_tool_command(data), _verify_command_for(track, next_verify))
            ):
                finish("allow", "exact_verifier_allowed", next_stage=next_verify)
                return
        detail = (
            "The conductor is permanently zero-write: only a spawned child session may edit "
            "workspace files. Direct-write tools and terminal write commands are prohibited; "
            "the conductor may run only the exact configured deterministic verifier when due. "
            "Next action: spawn the required child stage, or run the exact configured verifier when it is due."
        )
        if workspace_verifiers:
            detail += " allowed verifier argv: " + "; ".join(workspace_verifiers)
        if tool == "run_terminal_command":
            verdict = readonly_shell_verdict(_tool_command(data))
            code = "zero_write" if verdict == "write" else "shell_not_readonly"
        else:
            code = "zero_write" if tool in EDIT_TOOLS else "unknown_write_tool"
        operation_class = (
            classify_operation(_tool_command(data)) if tool == "run_terminal_command" else None
        )
        finish("deny", code, detail, operation_class=operation_class)
        return

    if mode != "dynamic":
        finish("allow", "mode_non_dynamic")
        return

    track = _load_execution(decision_id) if gate_active else None
    next_stage = _next_required_spawn_stage(track) if track else None
    recon_tool = tool in CONDUCTOR_RECON_TOOLS or (
        tool == "run_terminal_command" and is_recon_shell(_tool_command(data))
    )
    if gate_active and _is_recon_stage(track, next_stage) and recon_tool:
        detail = (
            _barrier_recipe(route, track, next_stage)
            if next_stage.kind == "parallel_spawn"
            else _stage_recipe(route, next_stage.role, track)
        )
        finish("deny", "recon_diet", detail, next_stage=next_stage.stage_id or next_stage.role)
        return
    finish("allow", "read_allowed" if tool in READ_TOOLS else "unmatched_tool_allowed")


def _debt_route(track: object, session_id: str | None) -> dict:
    intent = "implement"
    for stage in getattr(track, "stages", ()):
        roles = [stage.role]
        if stage.kind == "parallel_spawn":
            roles.extend(member.role for member in stage.members)
        for role in roles:
            for candidate, members in STATION_SKILLS.items():
                if role in members:
                    intent = candidate
                    return {"intent": intent, "session_id": session_id}
    return {"intent": intent, "session_id": session_id}


def _debt_recipe(track: object, session_id: str | None) -> str:
    route = _debt_route(track, session_id)
    next_stage = _next_required_spawn_stage(track)
    if next_stage is not None and next_stage.kind == "sentinel":
        # A sentinel role is not spawnable; a spawn recipe for it is unfollowable.
        recipe = next_stage.reason
    elif next_stage is not None:
        recipe = (
            _barrier_recipe(route, track, next_stage)
            if next_stage.kind == "parallel_spawn"
            else _stage_recipe(route, next_stage.role, track)
        )
    else:
        next_verify = track.next_verify_step()
        recipe = _verify_recipe(route, next_verify, track) if next_verify else _hold_recipe(route)
    marker = f" {STOP_FEEDBACK_MARK}."
    recipe = recipe.removesuffix(marker)
    if "Retrieve task results one id at a time" not in recipe:
        recipe += " Retrieve task results one id at a time."
    if next_stage is not None:
        key = getattr(next_stage, "role", "")
        tasks = getattr(track, "stage_tasks", {})
        bound_id = tasks.get(key)
        if next_stage.kind == "parallel_spawn":
            tasks = getattr(track, "member_tasks", {})
            prefix = f"{next_stage.stage_id}/"
            bound_id = next(
                (task_id for task_key, task_id in tasks.items() if task_key.startswith(prefix)),
                None,
            )
        if bound_id:
            recipe += f" Retrieve task {bound_id} results one id at a time to settle this stage."
    return f"repair debt: {recipe}{marker}"


def handle_stop(data: dict, spec: dict) -> None:
    session_id = _session_id(data)
    payload_sub = says_subagent(data)
    if payload_sub:
        transcript.ensure_subagent_pin(session_id)
    if _is_subagent(session_id) or payload_sub:
        _append_log(
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": TELEMETRY_GENERATION,
                "observed_at": _now(),
                "event": "stop",
                "session_id": session_id,
                "decision_id": f"subagent:{session_id or 'unknown'}",
                "turn_id": None,
                "blocked": False,
                "reason": str(data.get("reason") or ""),
                "reason_code": "subagent_exemption",
            }
        )
        return
    route = resolve_route(
        session_id,
        spec,
        persist=False,
        event="stop",
        workspace_root=data.get("workspaceRoot") or data.get("cwd"),
    )
    mode = route.get("mode") or "dynamic"
    decision_id = route.get("decision_id")
    reason = str(data.get("reason") or "")
    stop_active = bool(data.get("stopHookActive") or data.get("stop_hook_active"))
    has_strong = bool(route.get("has_strong"))
    if reason == "end_turn" and mode != "static":
        pending, blocked_found, recorded, defect, inconclusive = _sweep_pending_children(
            session_id, route
        )
        _append_log(
            {
                "version": SCHEMA_VERSION,
                "telemetry_generation": TELEMETRY_GENERATION,
                "observed_at": _now(),
                "event": "child_sweep",
                "session_id": session_id,
                "pending": pending,
                "blocked_found": blocked_found,
                "recorded": recorded,
                "defect": defect,
                "inconclusive": inconclusive,
                "classifications": {
                    "defect": defect,
                    "review_inconclusive": inconclusive,
                },
            },
            state=route.get("_runtime_state"),
        )
    gate_active = _gate_active(route, spec)
    stop_limit, debt_window = _stop_params(route)

    block = False
    detail = ""
    gate_lifted = False
    debt_decision_id = None
    remediation_context: dict[str, object] | None = None
    if reason == "end_turn" and mode == "dynamic" and gate_active:
        if decision_id:
            _ensure_track(route, decision_id)
        lifted, gate_detail = _enforceable_gate(decision_id, route)
        gate_lifted = lifted
        track = _load_execution(decision_id)
        if not lifted and track is not None and track.stop_blocks < stop_limit:
            block = True
            detail = gate_detail
            record_stop_block_tx(default_state_path(), decision_id)

    if reason == "end_turn" and mode == "dynamic" and not block:
        debt = load_state(default_state_path()).latest_session_debt(
            session_id, debt_window, excluded_decision_id=decision_id,
            max_stop_blocks=stop_limit,
        )
        if debt is not None:
            debt_decision_id, debt_track = debt
            if debt_track.stop_blocks < stop_limit:
                block = True
                detail = _debt_recipe(debt_track, session_id)
                record_stop_block_tx(default_state_path(), debt_decision_id)

    if reason == "end_turn" and mode == "dynamic":
        remediation = next(
            (
                record
                for record in open_remediation_debts(session_id, debt_window)
                if record.stop_blocks < stop_limit
            ),
            None,
        )
        if remediation is not None:
            remediation_context = remediation_stop_context(remediation)
            remediation_detail = "remediation debt: " + json.dumps(
                redact(remediation_context), ensure_ascii=False
            )
            detail = f"{detail} {remediation_detail}".strip()
            block = True
            record_remediation_stop(remediation)

    if reason != "end_turn":
        reason_code = "not_end_turn"
    elif mode != "dynamic":
        reason_code = "mode_non_dynamic"
    elif remediation_context is not None and block:
        reason_code = "remediation_debt"
    elif debt_decision_id and block:
        reason_code = "session_debt"
    elif block:
        reason_code = "pending_verify" if "verification" in detail else "pending_stage"
    elif gate_lifted:
        reason_code = "gate_lifted"
    elif not _declarative_gated(route, spec):
        reason_code = "intent_not_gated"
    elif not gate_active:
        reason_code = (
            "pipeline_complete" if not _pipeline_unfinished(decision_id, route) else "gate_lifted"
        )
    else:
        reason_code = "pipeline_hold"

    _append_log(
        {
            "version": SCHEMA_VERSION,
            "telemetry_generation": TELEMETRY_GENERATION,
            "observed_at": _now(),
            "event": "stop",
            "session_id": session_id,
            "intent": route.get("intent"),
            "role": route.get("role"),
            "required_model": route.get("required_model"),
            "required_effort": route.get("required_effort"),
            "current_model": route.get("current_model"),
            "has_strong": has_strong,
            "source": route.get("source"),
            "mode": mode,
            "gate_active": gate_active,
            "role_spawnable": route.get("role_spawnable", True),
            "execution_valid": route.get("execution_valid", True),
            "reason": reason,
            "reason_code": reason_code,
            "decision_id": decision_id,
            "debt_decision_id": debt_decision_id,
            "turn_id": route.get("turn_id"),
            "stop_hook_active": stop_active,
            "stop_block_limit": stop_limit,
            "blocked": block,
            "warnings": route.get("warnings", []),
            "detail_len": len(detail),
            **(
                {"remediation_debt": redact(remediation_context)}
                if remediation_context is not None
                else {}
            ),
        },
        state=route.get("_runtime_state"),
    )
    if block:
        _block_stop(detail)


def _route_debt_window(route: dict) -> float:
    try:
        profile = load_profiles().get(str(route.get("profile") or "default"))
        window = float(profile.debt_window_seconds) if profile is not None else 86400.0
        return window if window >= 0 else 86400.0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 86400.0


from grokbuild.settlement import configure as _configure_settlement  # noqa: E402


def _settlement_callback(name: str):
    def callback(*args, **kwargs):
        return globals()[name](*args, **kwargs)

    return callback


_configure_settlement(
    _append_log=_settlement_callback("_append_log"),
    _circuit_params=_settlement_callback("_circuit_params"),
    _consilium_threshold=_settlement_callback("_consilium_threshold"),
    _enforceable_gate=_settlement_callback("_enforceable_gate"),
    _gate_active=_settlement_callback("_gate_active"),
    _is_subagent=_settlement_callback("_is_subagent"),
    _is_verify_command=_settlement_callback("_is_verify_command"),
    _load_execution=_settlement_callback("_load_execution"),
    _next_required_spawn_role=_settlement_callback("_next_required_spawn_role"),
    _route_debt_window=_settlement_callback("_route_debt_window"),
    _session_id=_settlement_callback("_session_id"),
    _stop_params=_settlement_callback("_stop_params"),
    _tool_command=_settlement_callback("_tool_command"),
    _verify_command_for=_settlement_callback("_verify_command_for"),
    resolve_route=_settlement_callback("resolve_route"),
)


def main() -> int:
    data = _read_stdin()
    event = _event_name(data) or str(os.environ.get("GROK_HOOK_EVENT") or "").casefold()
    if _STDIN_UNPARSEABLE and event in {"pre_tool_use", "pretooluse"}:
        _finish_pre_tool(
            "deny",
            "stdin_unparseable",
            session_id=None,
            tool="",
            decision_id=None,
            turn_id=None,
            intent=None,
            role=None,
            model=None,
            effort=None,
            mode=None,
            gate_active=False,
            detail="hook payload was not valid JSON; only read tools allowed",
        )
        return 0
    if event in {"post_tool_use", "posttooluse"}:
        _dump_payload_debug(data)
    try:
        spec = load_intents()
        spec = apply_config_verifiers(spec)
    except (OSError, json.JSONDecodeError):
        session_id = _session_id(data)
        decision_id = hash_id("routing_config_unavailable", session_id or "unknown")
        if event in {"pre_tool_use", "pretooluse"}:
            tool = str(data.get("toolName") or data.get("tool_name") or "").casefold()
            outcome = "allow" if tool in READ_TOOLS else "deny"
            _finish_pre_tool(
                outcome,
                "routing_config_unavailable",
                session_id=session_id,
                tool=tool,
                decision_id=decision_id,
                turn_id=None,
                intent=None,
                role=None,
                model=None,
                effort=None,
                mode=None,
                gate_active=False,
                detail="routing configuration unavailable; only read tools are allowed",
            )
        elif event == "stop":
            _append_log(
                {
                    "version": SCHEMA_VERSION,
                    "telemetry_generation": TELEMETRY_GENERATION,
                    "observed_at": _now(),
                    "event": "stop",
                    "session_id": session_id,
                    "decision_id": decision_id,
                    "turn_id": None,
                    "blocked": False,
                    "reason": str(data.get("reason") or ""),
                    "reason_code": "routing_config_unavailable",
                }
            )
        return 0
    if event in {"user_prompt_submit", "userpromptsubmit", "before_submit_prompt"}:
        handle_prompt(data, spec)
        return 0
    if event in {"pre_tool_use", "pretooluse"}:
        try:
            handle_pre_tool(data, spec)
        except Exception:
            tool = str(data.get("toolName") or data.get("tool_name") or "").casefold()
            if tool in READ_TOOLS:
                print(json.dumps({"decision": "allow"}))
            else:
                print(
                    json.dumps(
                        {
                            "decision": "deny",
                            "reason": "hook internal error; failing closed for non-read tools",
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
        return 0
    if event in {"post_tool_use", "posttooluse"}:
        try:
            handle_post_tool(data, spec)
        except Exception:
            return _handle_failure(data, spec, "post_tool_use")
        return 0
    if event in {"post_tool_use_failure", "posttoolusefailure"}:
        try:
            handle_post_tool_failure(data, spec)
        except Exception:
            return _handle_failure(data, spec, "post_tool_use_failure")
        return 0
    if event in {"stop"}:
        try:
            handle_stop(data, spec)
        except Exception:
            return _handle_failure(data, spec, "stop")
        return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
