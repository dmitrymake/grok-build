#!/usr/bin/env python3
"""CLI for the Grok Build combine router: explain / state / replay.

Usage (from the routing dir, or anywhere with this file on PYTHONPATH):

    grok-route explain "find RCE in demo-api" [--json]
    grok-route state [--json]
    grok-route state set --role security --available false --reason "z.ai down"
    grok-route state set --role implement --quota-used 90 --quota-limit 100
    grok-route state prune --yes [--older-than SECONDS]
    grok-route replay [--last 20] [--session SID] [--reconstruct-state]
        (reconstruct-state cannot be combined with --last or --session)
    grok-route roles
    grok-route stats [--json] [--since ISO] [--session SID] [--repo ID]
    grok-route profiles
    grok-route visual-config
    grok-route visual-validate < result.json
    grok-route visual-cache stats|clear [--yes]
    grok-route models sync|list|diff|quota [--json]
    grok-route contract-check [--capture PATH]
    grok-route version

`explain` is static (no state, no logs). `replay` reads the redacted outcome
JSONL — prompts are intentionally absent, so replay reconstructs/augments state
from recorded decisions rather than re-classifying original text.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ._repo import config_path
from typing import Any

from grokbuild.conductor import resolve_conductor
from grokbuild.decision import SCHEMA_VERSION, decision_to_dict
from grokbuild.persist import state_lock
from grokbuild.transactions import transaction
from grokbuild.policy import load_profiles
from grokbuild.remediation import compact_remediation_debt, remediation_path
from grokbuild.roles import load_registry
from grokbuild.router import route_prompt
from grokbuild.state import (
    RuntimeState,
    default_log_path,
    default_state_path,
    load_state,
    save_state,
)
from grokbuild.transactions import record_stage_tx


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None):
        return " ".join(args.prompt)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


_CONTRACT_TOOLS = ("get_command_or_subagent_output", "run_terminal_command")
_PROBE_INSTRUCTIONS = (
    "Probe: touch ~/.local/state/grok-route/payload-debug.enabled, run one background "
    "run_terminal_command, then retrieve it with get_command_or_subagent_output."
)


def _latest_captured_payloads(
    path: Path, tool_names: tuple[str, ...]
) -> tuple[dict[str, dict], int]:
    latest: dict[str, dict] = {}
    valid_records = 0
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return latest, valid_records
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            valid_records += 1
            tool_name = payload.get("toolName")
            if tool_name == "run_terminal_command":
                request = payload.get("toolInput")
                if not isinstance(request, dict) or request.get("background") is not True:
                    continue
            if tool_name in tool_names:
                latest[tool_name] = payload
    return latest, valid_records


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return type(value).__name__


# host-injected session-identity marker, not part of the payload contract.
_SKELETON_IGNORED_KEYS = frozenset({"subagentType"})


def _skeleton_differences(expected: object, actual: object, path: str) -> list[str]:
    expected_type = _json_type(expected)
    actual_type = _json_type(actual)
    if expected_type != actual_type:
        return [f"{path}: wrong type (expected {expected_type}, got {actual_type})"]
    if isinstance(expected, dict):
        differences: list[str] = []
        expected_keys = set(expected) - _SKELETON_IGNORED_KEYS
        actual_keys = set(actual) - _SKELETON_IGNORED_KEYS
        differences.extend(
            f"{path}: missing key {key!r}" for key in sorted(expected_keys - actual_keys)
        )
        differences.extend(
            f"{path}: extra key {key!r}" for key in sorted(actual_keys - expected_keys)
        )
        for key in sorted(expected_keys & actual_keys):
            differences.extend(_skeleton_differences(expected[key], actual[key], f"{path}.{key}"))
        return differences
    if isinstance(expected, list):
        if expected and not actual:
            return [f"{path}: missing list element shape"]
        if not expected and actual:
            return [f"{path}: extra list element shape"]
        if not expected:
            return []
        differences = []
        for index, item in enumerate(actual):
            differences.extend(_skeleton_differences(expected[0], item, f"{path}[{index}]"))
        return differences
    return []


def _contract_fixture(name: str) -> dict:
    path = Path(__file__).resolve().parent / "fixtures" / "task-payloads" / name
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_contract_check(args: argparse.Namespace) -> int:
    """Validate captured tool payloads against the contract fixtures."""
    capture = (
        Path(args.capture)
        if getattr(args, "capture", None)
        else default_log_path().parent / "payload-debug.jsonl"
    )
    latest, valid_records = _latest_captured_payloads(capture, _CONTRACT_TOOLS)
    retrieval_name = "get_command_or_subagent_output"
    terminal_name = "run_terminal_command"
    if not capture.is_file():
        print(f"payload capture is missing: {capture}", file=sys.stderr)
        print(_PROBE_INSTRUCTIONS, file=sys.stderr)
        return 1
    if not valid_records:
        print(f"payload capture has no valid records: {capture}", file=sys.stderr)
        print(_PROBE_INSTRUCTIONS, file=sys.stderr)
        return 1
    if retrieval_name not in latest:
        print("payload capture has no get_command_or_subagent_output record", file=sys.stderr)
        print(_PROBE_INSTRUCTIONS, file=sys.stderr)
        return 1

    expected_retrieval = _contract_fixture("retrieval-captured-single.json")
    differences = _skeleton_differences(expected_retrieval, latest[retrieval_name], retrieval_name)
    retrieval_result = latest[retrieval_name].get("toolResult")
    retrieval_type = retrieval_result.get("type") if isinstance(retrieval_result, dict) else None
    if retrieval_type != "TaskOutput":
        differences.append(
            f"{retrieval_name}.toolResult.type: invariant value "
            f'(expected "TaskOutput", got type {_json_type(retrieval_type)})'
        )
    if terminal_name in latest:
        expected_terminal = _contract_fixture("terminal-background-captured.json")
        differences.extend(
            _skeleton_differences(expected_terminal, latest[terminal_name], terminal_name)
        )
        terminal_result = latest[terminal_name].get("toolResult")
        terminal_type = terminal_result.get("type") if isinstance(terminal_result, dict) else None
        if terminal_type != "BackgroundTaskStarted":
            differences.append(
                f"{terminal_name}.toolResult.type: invariant value "
                f'(expected "BackgroundTaskStarted", got type {_json_type(terminal_type)})'
            )
    if differences:
        for difference in sorted(set(differences)):
            print(difference, file=sys.stderr)
        return 1
    print("OK: latest payload contracts match")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Classify a prompt and emit its routing decision."""
    prompt = _read_prompt(args)
    if not prompt.strip():
        print("no prompt (pass text as arguments or on stdin)", file=sys.stderr)
        return 2
    decision = route_prompt(
        prompt,
        mode=args.mode or "static",
        profile_name=args.profile,
        registry=load_registry(config_path()),
        current_model=args.current_model,
        event="explain",
        persist=False,
    )
    payload = decision_to_dict(decision)
    if not args.json:
        # Human summary on stderr, machine JSON on stdout (always).
        print(
            f"intent={decision.intent} role={decision.role} model={decision.model} "
            f"reason={decision.reason} score={decision.score:g} mode={decision.mode} "
            f"allowed={decision.allowed} would_deny_edits={decision.would_deny_edits} "
            f"would_block_stop={decision.would_block_stop}",
            file=sys.stderr,
        )
        for stage, value in decision.stage_trace:
            print(f"  {stage}: {value}", file=sys.stderr)
    _emit(payload)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Emit the current runtime state and provider availability."""
    state = load_state(default_state_path())
    payload = state.to_dict()
    payload["provider_availability_status"] = state.provider_availability_snapshot()
    _emit(payload)
    return 0


def _parse_until(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--until must be a valid ISO8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--until must include a timezone")
    return parsed.astimezone(UTC).timestamp()


def _next_monday_utc(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    days = (7 - now.weekday()) % 7
    if days == 0:
        days = 7
    return (
        (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )


def cmd_provider(args: argparse.Namespace) -> int:
    """Update and report provider quota availability."""
    until = args.until if args.action == "quota-exhausted" else None
    if args.action == "quota-exhausted" and until is None:
        until = _next_monday_utc()

    def update(state: RuntimeState) -> None:
        if args.action == "quota-exhausted":
            state.set_provider_unavailable(args.name, float(until), "quota-exhausted")
        else:
            state.clear_provider_unavailable(args.name)

    transaction(default_state_path(), update)
    state = load_state(default_state_path())
    _emit(state.provider_availability_snapshot().get(args.name, {}))
    return 0


def cmd_state_set(args: argparse.Namespace) -> int:
    """Update role availability, failures, or quota counters."""
    role = args.role
    quota_used = args.quota_used
    quota_limit = args.quota_limit
    if (
        args.available is None
        and args.failures is None
        and quota_used is None
        and quota_limit is None
    ):
        print(
            "nothing to do (use --available true|false, --failures N, "
            "--quota-used N, --quota-limit N)",
            file=sys.stderr,
        )
        return 2
    if quota_used is not None and quota_used < 0:
        print("--quota-used must be nonnegative", file=sys.stderr)
        return 2
    if quota_limit is not None and quota_limit < 0:
        print("--quota-limit must be nonnegative", file=sys.stderr)
        return 2

    def update(state: RuntimeState) -> None:
        status = state.status_for(role)
        if args.available is not None:
            state.set_available(role, args.available == "true", args.reason or "")
        if args.failures is not None:
            failures = int(args.failures)
            if failures < 0:
                raise ValueError("--failures must be nonnegative")
            status.consecutive_failures = failures
            status.total_failures = max(status.total_failures, failures)
            if failures > 0:
                status.available = False
                status.reason = args.reason or f"marked {failures} consecutive failures"
        if quota_used is not None:
            status.quota_used = quota_used
        if quota_limit is not None:
            status.quota_limit = quota_limit
        if status.quota_limit is not None and status.quota_used > status.quota_limit:
            raise ValueError(
                f"quota_used ({status.quota_used}) exceeds quota_limit ({status.quota_limit})"
            )

    try:
        transaction(default_state_path(), update)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _emit(load_state(default_state_path()).status_for(role).to_dict())
    return 0


def _sidecar_report(directory: Path) -> dict[str, Any]:
    """Return the on-disk size of every sidecar generation in the state directory."""
    report: dict[str, Any] = {}
    for name in (
        "state.json",
        "decisions.jsonl",
        "route.jsonl",
        "evidence-v1.jsonl",
        "endpoint-resolution-v1.jsonl",
        "remediation-debt-v1.jsonl",
        "payload-debug.jsonl",
    ):
        sizes = {}
        for candidate in (directory / name, *(directory.glob(f"{name}.[0-9]"))):
            if candidate.is_file():
                sizes[candidate.name] = candidate.stat().st_size
        if sizes:
            report[name] = sizes
    return report


def cmd_state_prune(args: argparse.Namespace) -> int:
    """Compact the state file and its sidecars to their bounded form."""
    if not args.yes:
        print("state prune requires --yes", file=sys.stderr)
        return 2
    if args.older_than is not None and args.older_than <= 0:
        print("--older-than must be positive", file=sys.stderr)
        return 2
    path = default_state_path()
    directory = path.parent
    before_state = load_state(path)
    before = {
        "executions": len(before_state.executions),
        "turns": len(before_state.turns),
        "session_circuits": len(before_state.session_circuits),
        "sidecars": _sidecar_report(directory),
    }
    ttl = args.older_than

    def update(state: RuntimeState) -> None:
        if ttl is not None:
            cutoff = time.time() - ttl
            for decision_id in [
                key for key, track in state.executions.items() if track.updated_at < cutoff
            ]:
                state.executions.pop(decision_id, None)
        # prune() runs inside the transaction anyway; the explicit TTL above is
        # the operator's tighter-than-default lever, never a looser one.
        state.prune()

    transaction(path, update)
    remediation_target = remediation_path()
    with state_lock(remediation_target, lock_name="remediation-debt-v1.lock"):
        debt_before, debt_after = compact_remediation_debt(remediation_target)
    after_state = load_state(path)
    _emit(
        {
            "state_path": str(path),
            "before": before,
            "after": {
                "executions": len(after_state.executions),
                "turns": len(after_state.turns),
                "session_circuits": len(after_state.session_circuits),
                "sidecars": _sidecar_report(directory),
            },
            "remediation_records": {"before": debt_before, "after": debt_after},
        }
    )
    return 0


def complete_execution(
    path: Path, decision_id: str, role: str, failure: bool = False
) -> dict[str, Any]:
    """Record completion or failure for an execution stage."""
    track = load_state(path).get_execution(decision_id)
    if track is None:
        raise ValueError(f"unknown decision_id {decision_id!r}; valid roles/member keys: none")
    valid = {
        stage.role
        for stage in track.stages
        if stage.kind != "parallel_spawn" and stage.spawnable and stage.role
    } | {
        track.member_key(stage.stage_id, member.member_id)
        for stage in track.stages
        if stage.kind == "parallel_spawn"
        for member in stage.members
    }
    if role not in valid:
        choices = ", ".join(sorted(valid)) or "none"
        raise ValueError(
            f"unknown role/member key {role!r} for decision {decision_id!r}; "
            f"valid roles/member keys: {choices}"
        )
    record_stage_tx(path, decision_id, "result", role=role, success=not failure)
    track = load_state(path).get_execution(decision_id)
    return {
        "decision_id": decision_id,
        "completed": list(track.completed),
        "failed": list(track.failed),
    }


def cmd_complete(args: argparse.Namespace) -> int:
    """Record a command-line execution-stage result."""
    try:
        payload = complete_execution(
            default_state_path(), args.decision_id, args.role, args.failure
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _emit(payload)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay routing telemetry and optionally reconstruct runtime state."""
    path = default_log_path()
    if not path.is_file():
        print(json.dumps({"records": [], "summary": {}}, ensure_ascii=False, indent=2))
        return 0
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if args.session and item.get("session_id") != args.session:
                continue
            records.append(item)
    if args.last:
        records = records[-int(args.last) :]

    summary: dict[str, Any] = {"total": len(records)}
    counts: dict[str, int] = {}
    for item in records:
        key = f"{item.get('intent') or 'none'}→{item.get('role') or 'none'}"
        counts[key] = counts.get(key, 0) + 1
    summary["by_intent_role"] = dict(sorted(counts.items()))

    if args.reconstruct_state:
        state = RuntimeState(source_path=default_state_path())
        for item in records:
            state.record_decision(item)
        save_state(state, default_state_path())
        summary["reconstructed_state"] = str(default_state_path())

    _emit({"records": records, "summary": summary})
    return 0


def cmd_roles(_args: argparse.Namespace) -> int:
    """Emit the validated role registry and intent mappings."""
    from grokbuild.classify import load_intents

    # Validate the config adjacent to the invoked CLI. Repo invocations inspect
    # the checkout; installed ~/.grok invocations inspect the live config.
    registry = load_registry(config_path())
    payload = {
        "registry": registry.as_dict(),
        "validation": {
            "roles": registry.validate_roles(),
            "intents": registry.validate_intents(load_intents()),
        },
    }
    _emit(payload)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Compute and emit routing telemetry statistics."""
    from grokbuild.stats import compute_stats, human_summary

    try:
        payload = compute_stats(
            log=args.log,
            labels=args.labels,
            since=args.since,
            session=args.session,
            repo=args.repo,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        _emit(payload)
    else:
        print(human_summary(payload))
    return 0


def cmd_profiles(_args: argparse.Namespace) -> int:
    """Emit the configured routing profiles."""
    profiles = load_profiles()
    _emit({name: profile.to_dict() for name, profile in sorted(profiles.items())})
    return 0


def cmd_conductor(args: argparse.Namespace) -> int:
    """Resolve and emit the pre-session conductor handoff."""
    handoff = resolve_conductor()
    if getattr(args, "model_only", False):
        print(handoff.model)
        return 0
    _emit(handoff.to_dict())
    return 0


def cmd_visual_config(_args: argparse.Namespace) -> int:
    """Emit visual-intake policy and its resolved role chain."""
    from grokbuild.visual_intake import load_visual_intake_policy

    config = config_path()
    policy = load_visual_intake_policy(config)
    registry = load_registry(config)
    primary = registry.get("visual-intake")
    chain = []
    if primary:
        for name in (primary.name, *primary.fallback):
            role = registry.get(name)
            if role:
                chain.append(
                    {
                        "role": role.name,
                        "model": role.model,
                        "capabilities": role.capabilities.to_dict(),
                    }
                )
    _emit({**policy.to_dict(), "resolved_chain": chain})
    return 0


def cmd_visual_validate(_args: argparse.Namespace) -> int:
    """Validate a visual-intake result read from standard input."""
    from grokbuild.visual_intake import validate_visual_intake_result

    try:
        payload = json.load(sys.stdin)
        result = validate_visual_intake_result(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _emit({"valid": False, "error": str(exc)})
        return 2
    _emit({"valid": True, "result": result.to_dict()})
    return 0


def cmd_visual_cache(args: argparse.Namespace) -> int:
    """Inspect or explicitly clear the visual-intake cache."""
    from grokbuild.visual_cache import visual_cache_clear, visual_cache_stats

    if args.action == "clear":
        if not args.yes:
            print("visual-cache clear requires --yes", file=sys.stderr)
            return 2
        visual_cache_clear()
    _emit(visual_cache_stats())
    return 0


def _format_age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "never"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _quota_summary(quota: Any) -> str:
    if not isinstance(quota, dict):
        return "-"
    windows = quota.get("windows")
    if not isinstance(windows, list):
        return "-"
    parts = [
        f"{w.get('name')}:{100 - float(w['used_percent']):g}%left"
        for w in windows
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))
    ]
    return ",".join(parts) or "-"


def cmd_models(args: argparse.Namespace) -> int:
    """List, synchronize, compare, or report quotas for upstream models."""
    import time

    from grokbuild.discovery import (
        diff,
        load_cache,
        load_targets,
        quota_report,
        stale_targets,
        sync,
    )

    targets, warnings = load_targets()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.models_command == "sync":
        if_stale = getattr(args, "if_stale", None)
        if if_stale is not None:
            if if_stale < 0:
                print("--if-stale must be nonnegative", file=sys.stderr)
                return 2
            targets = stale_targets(targets, load_cache(), if_stale * 60)
        result = sync(targets)
        if args.json:
            _emit(result)
            return 0
        for name, entry in sorted(result["providers"].items()):
            count = len(entry.get("models", [])) if entry.get("status") == "ok" else "-"
            print(
                f"provider={name} status={entry.get('status')} "
                f"http={entry.get('http_status') or '-'} models={count} "
                f"quota={_quota_summary(entry.get('quota'))}"
            )
        return 0

    cache = load_cache()
    if args.models_command == "quota":
        report = quota_report(targets, cache)
        if args.json:
            _emit(report)
            return 0
        for name, entry in sorted(report["providers"].items()):
            if not entry.get("supported"):
                print(f"provider={name} quota=n/a (no known usage endpoint)")
                continue
            quota = entry.get("quota")
            if not quota:
                print(f"provider={name} quota=unknown (run: cli.py models sync)")
                continue
            age = _format_age(entry.get("age_seconds"))
            plan = quota.get("plan")
            head = f"provider={name} age={age}" + (f" plan={plan}" if plan else "")
            print(head)
            for window in quota.get("windows") or []:
                used = window.get("used_percent")
                if not isinstance(used, (int, float)):
                    continue
                resets = window.get("resets_at") or "-"
                print(
                    f"  {window.get('name')}: used={used:g}% remaining={100 - used:g}% resets_at={resets}"
                )
        return 0
    if args.models_command == "list":
        if args.json:
            _emit(cache)
            return 0
        now = time.time()
        providers = cache.get("providers", {})
        if not providers:
            print("models cache empty; run: cli.py models sync")
            return 0
        for name, entry in sorted(providers.items()):
            fetched = entry.get("fetched_at")
            age = _format_age(now - fetched if isinstance(fetched, (int, float)) else None)
            models = entry.get("models") or []
            print(f"provider={name} status={entry.get('status')} age={age} models={len(models)}")
            for model_id in models:
                print(f"  {model_id}")
        return 0

    report = diff(targets, cache)
    if args.json:
        _emit(report)
        return 0
    exit_code = 0
    for name, entry in sorted(report["providers"].items()):
        age = _format_age(entry.get("age_seconds"))
        stale = " STALE" if entry.get("stale") else ""
        print(f"provider={name} status={entry.get('status')} age={age}{stale}")
        if entry.get("missing_upstream") is None:
            print("  (no upstream data; run: cli.py models sync)")
            continue
        for model_id in entry["missing_upstream"]:
            print(f"  dead-pin: {model_id} configured but absent upstream")
            exit_code = 1
        unbound = entry.get("unbound") or []
        if unbound:
            print(f"  unbound ({len(unbound)}): {', '.join(unbound)}")
    return exit_code


def cmd_version(_args: argparse.Namespace) -> int:
    """Emit the route-decision schema version."""
    _emit({"route_decision_schema_version": SCHEMA_VERSION})
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(prog="grok-route", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_explain = sub.add_parser("explain", help="static classification (no state, no logs)")
    p_explain.add_argument("prompt", nargs="*")
    p_explain.add_argument("--json", action="store_true")
    p_explain.add_argument("--mode", choices=["static", "shadow", "dynamic"])
    p_explain.add_argument("--profile")
    p_explain.add_argument("--current-model")
    p_explain.set_defaults(func=cmd_explain)

    p_state = sub.add_parser("state", help="show runtime state")
    p_state.set_defaults(func=cmd_state)
    state_sub = p_state.add_subparsers(dest="state_command")
    p_set = state_sub.add_parser("set", help="mutate role availability/failures/quota")
    p_set.add_argument("--role", required=True)
    p_set.add_argument("--available", choices=["true", "false"])
    p_set.add_argument("--reason", default="")
    p_set.add_argument("--failures", type=int)
    p_set.add_argument("--quota-used", type=int, help="set quota_used (nonnegative)")
    p_set.add_argument("--quota-limit", type=int, help="set quota_limit (nonnegative)")
    p_set.set_defaults(func=cmd_state_set)
    p_prune = state_sub.add_parser("prune", help="compact state.json and its sidecars")
    p_prune.add_argument("--yes", action="store_true", help="confirm the destructive compaction")
    p_prune.add_argument(
        "--older-than",
        type=float,
        metavar="SECONDS",
        help="also drop execution tracks older than this (tighter than the 24h TTL)",
    )
    p_prune.set_defaults(func=cmd_state_prune)

    p_complete = sub.add_parser("complete", help="manually record a stage result")
    p_complete.add_argument("decision_id")
    p_complete.add_argument("role")
    p_complete.add_argument("--failure", action="store_true")
    p_complete.set_defaults(func=cmd_complete)

    p_contract = sub.add_parser("contract-check", help="validate captured task payload structures")
    p_contract.add_argument("--capture", help="payload-debug JSONL path")
    p_contract.set_defaults(func=cmd_contract_check)

    p_provider = sub.add_parser("provider", help="set provider quota availability")
    p_provider.add_argument("name")
    p_provider.add_argument("action", choices=["quota-exhausted", "available"])
    p_provider.add_argument("--until", type=_parse_until)
    p_provider.set_defaults(func=cmd_provider)

    p_replay = sub.add_parser(
        "replay",
        help="replay redacted outcome JSONL (state reconstruction cannot be combined with filters)",
    )
    p_replay.add_argument("--last", type=int)
    p_replay.add_argument("--session")
    p_replay.add_argument("--reconstruct-state", action="store_true")
    p_replay.set_defaults(func=cmd_replay)

    sub.add_parser("roles", help="show validated role registry").set_defaults(func=cmd_roles)

    p_stats = sub.add_parser("stats", help="summarize schema-v4 routing telemetry")
    p_stats.add_argument("--json", action="store_true")
    p_stats.add_argument("--since")
    p_stats.add_argument("--session")
    p_stats.add_argument("--repo")
    p_stats.add_argument("--log")
    p_stats.add_argument("--labels")
    p_stats.set_defaults(func=cmd_stats)

    sub.add_parser("profiles", help="show routing profiles").set_defaults(func=cmd_profiles)
    sub.add_parser(
        "visual-config", help="show visual-intake policy and binding chain"
    ).set_defaults(func=cmd_visual_config)
    sub.add_parser(
        "visual-validate", help="validate visual-intake schema v1 from stdin"
    ).set_defaults(func=cmd_visual_validate)
    p_visual_cache = sub.add_parser(
        "visual-cache", help="inspect or explicitly clear visual-intake cache"
    )
    p_visual_cache.add_argument("action", choices=["stats", "clear"])
    p_visual_cache.add_argument("--yes", action="store_true")
    p_visual_cache.set_defaults(func=cmd_visual_cache)

    p_models = sub.add_parser(
        "models", help="upstream model discovery (advisory cache, no pin changes)"
    )
    p_models.add_argument("--json", action="store_true")
    models_sub = p_models.add_subparsers(dest="models_command")
    p_models.set_defaults(func=cmd_models, models_command="list")
    p_models_sync = models_sub.add_parser(
        "sync", help="fetch GET /models per credentialed provider into the cache"
    )
    p_models_sync.add_argument("--json", action="store_true")
    p_models_sync.add_argument(
        "--if-stale",
        type=float,
        metavar="MINUTES",
        help="sync only targets whose cached quota/model signal is older than MINUTES",
    )
    p_models_sync.set_defaults(func=cmd_models)
    p_models_list = models_sub.add_parser("list", help="show the cached upstream model lists")
    p_models_list.add_argument("--json", action="store_true")
    p_models_list.set_defaults(func=cmd_models)
    p_models_diff = models_sub.add_parser(
        "diff", help="compare configured pins with cached upstream lists"
    )
    p_models_diff.add_argument("--json", action="store_true")
    p_models_diff.set_defaults(func=cmd_models)
    p_models_quota = models_sub.add_parser("quota", help="show cached remaining quota per provider")
    p_models_quota.add_argument("--json", action="store_true")
    p_models_quota.set_defaults(func=cmd_models)

    p_conductor = sub.add_parser(
        "conductor", help="resolve the pre-session conductor (typed handoff)"
    )
    p_conductor.add_argument(
        "--model-only", action="store_true", help="print only the model id (for launchers)"
    )
    p_conductor.set_defaults(func=cmd_conductor)

    sub.add_parser("version", help="show RouteDecision schema version").set_defaults(
        func=cmd_version
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and dispatch the selected command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "reconstruct_state", False) and (
        getattr(args, "last", None) is not None or getattr(args, "session", None)
    ):
        parser.error("--reconstruct-state cannot be combined with --last or --session")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
