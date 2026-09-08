"""Gate evaluation and station recipes."""

from __future__ import annotations

import grokbuild.roles as roles


import json
import math
import time
from pathlib import Path

from grokbuild.classify import STOP_FEEDBACK_MARK, load_intents
from grokbuild.verifiers import is_verifier_command
from grokbuild.policy import gated_intents, load_profiles
from grokbuild.roles import ROLE_ALIASES, load_registry

from grokbuild.state import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    load_state,
    default_state_path,
)
from grokbuild.transactions import ensure_execution_tx
from grokbuild.transcript import (
    STATION_SKILLS,
    _role_availability_verified,
    last_user_prompt,
)

READ_ONLY_ROLES = roles.EXECUTION_READ_ONLY_ROLES

PROMPT_SNIPPET = 400

_STATION_FALLBACK_FALLBACK = {
    "visual-intake": ("minimax-m3", None),
    "visual-intake-deep": ("gpt-5.6-terra", None),
    "security": ("glm-5.3", "max"),
    "security-verify": ("grok-4.6", None),
    "implement": ("gpt-5.6-luna", "max"),
    "implement-cheap": ("gpt-5.6-luna", "high"),
    "implement-standard": ("gpt-5.6-luna", "max"),
    "implement-hard": ("gpt-6-astra", "max"),
    "implement-ops": ("glm-5.3", "high"),
    "implement-overflow": ("deepseek-v4-pro", "high"),
    "implement-cheap-fallback": ("deepseek-v4-flash", "high"),
    "review": ("gpt-5.6-sol", "xhigh"),
    "review-hard": ("glm-5.3", "max"),
    "review-independent": ("grok-4.6", None),
    "expert-rescue": ("deepseek-v4-pro", "high"),
    "explore": ("gpt-5.6-luna", "high"),
    "explore-thorough": ("gpt-5.6-luna", "max"),
    "explore-risk": ("minimax-m3", "high"),
    "plan": ("gpt-6-astra", "max"),
    "plan-hard": ("gpt-6-astra", "max"),
    "implement-strong": ("gpt-5.6-sol", "max"),
    "planner-strong": ("gpt-5.6-sol", "max"),
}


def _load_station_fallback() -> dict[str, tuple[str, str | None]]:
    try:
        data = json.loads(
            (Path(__file__).resolve().parent / "station_fallback.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict) and data:
            out = {}
            for role, pair in data.items():
                if (
                    isinstance(pair, list)
                    and len(pair) == 2
                    and isinstance(pair[0], str)
                    and (pair[1] is None or isinstance(pair[1], str))
                ):
                    out[str(role)] = (pair[0], pair[1])
            if len(out) == len(data):
                return out
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return _STATION_FALLBACK_FALLBACK.copy()


STATION_FALLBACK = _load_station_fallback()

_REGISTRY_CACHE = None


def _role_pair(role: str) -> tuple[str | None, str | None]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        try:
            _REGISTRY_CACHE = load_registry()
        except Exception:  # noqa: BLE001
            _REGISTRY_CACHE = False
    if _REGISTRY_CACHE is not False:
        role_obj = _REGISTRY_CACHE.get(role)
        if role_obj is not None and role_obj.model:
            return role_obj.model, role_obj.reasoning_effort
    return STATION_FALLBACK.get(role, (None, None))


def _role_autonomy(role: str) -> str:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE not in (None, False):
        role_obj = _REGISTRY_CACHE.get(role)
        if role_obj is not None:
            return role_obj.autonomy
    return "standard"


def _format_pair(role: str) -> str:
    model, effort = _role_pair(role)
    autonomy = _role_autonomy(role)
    brief = _brief_contract(role)
    if not model:
        return f"pinned role [autonomy={autonomy}; {brief}]"
    pin = f"{model} @ {effort}" if effort else model
    return f"{pin} [autonomy={autonomy}; {brief}]"


def _brief_contract(role: str) -> str:
    autonomy = _role_autonomy(role)
    if autonomy == "guided":
        return "guided brief: include exact files, steps, acceptance, and verifier"
    if autonomy == "broad":
        return "broad brief: provide goals and constraints; read-only investigation may self-direct within scope"
    return "standard brief: provide goals, constraints, acceptance, and bounded local discretion"


def _verify_command_for(track: object, step: str) -> str:
    if track is None:
        return ""
    for stage in getattr(track, "stages", ()):
        identity = stage.stage_id or stage.role
        if identity == step and not stage.spawnable:
            return stage.command
    return ""


def _is_verify_command(command: str | None, verify_command: str) -> bool:
    """True only when the shell command shlex-parses to exactly the configured argv.

    No wrappers, extra args, metacharacters, or alternate relative paths are
    accepted — the deterministic verify step is the exact configured command.
    """
    return is_verifier_command(command, verify_command)


def _prompt_snippet(session_id: str | None, limit: int = PROMPT_SNIPPET) -> str:
    text = last_user_prompt(session_id)
    text = " ".join(text.split()).replace(STOP_FEEDBACK_MARK, "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def station_recipe(route: dict) -> str:
    """Build the next-stage station instruction for a route."""
    intent = str(route.get("intent") or "")
    role = ROLE_ALIASES.get(intent, intent)
    pair = _format_pair(role)
    if pair == "pinned role" and route.get("required_model"):
        effort = route.get("required_effort")
        pair = f"{route['required_model']} @ {effort}" if effort else str(route["required_model"])
    snippet = _prompt_snippet(route.get("session_id"))
    prompt = snippet or f"Do the {intent} work from the last user turn."
    capability = 'and capability_mode="all" ' if intent == "implement" else ""
    return (
        f'route={intent}: NEXT: call spawn_subagent with subagent_type="{intent}" '
        f"{capability}"
        f'using the pinned {pair} role and prompt "{prompt}". '
        f"Stage Gating: normal lifecycle coordination; next required stage is the spawned station. The conductor remains permanently zero-write; only child sessions may edit. "
        f"{STOP_FEEDBACK_MARK}."
    )


def _circuit_params(route: dict) -> tuple[int, float]:
    """Profile-configured circuit threshold/cooldown, with safe defaults."""
    profile_name = str(route.get("profile") or "default")
    try:
        profiles = load_profiles()
    except (OSError, json.JSONDecodeError):
        profiles = {}
    profile = profiles.get(profile_name)
    if profile is None:
        return DEFAULT_FAILURE_THRESHOLD, DEFAULT_COOLDOWN_SECONDS
    return profile.circuit_failure_threshold, profile.circuit_cooldown_seconds


def _consilium_threshold(route: dict) -> int:
    """Profile-configured consecutive repair failure threshold."""
    try:
        profile = load_profiles().get(str(route.get("profile") or "default"))
        value = int(profile.consilium_after_failures) if profile is not None else 3
        return value if value > 0 else 3
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 3


def _stop_params(route: dict) -> tuple[int, float]:
    """Profile-configured Stop limit and debt window, with safe defaults."""
    try:
        profile = load_profiles().get(str(route.get("profile") or "default"))
        limit = int(profile.stop_block_limit) if profile is not None else 3
        window = float(profile.debt_window_seconds) if profile is not None else 86400.0
        if limit < 0:
            limit = 3
        if not math.isfinite(window) or window < 0:
            window = 86400.0
        return limit, window
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 3, 86400.0


def _barrier_stall_seconds(route: dict) -> float:
    try:
        profile = load_profiles().get(str(route.get("profile") or "default"))
        value = float(profile.barrier_stall_seconds) if profile is not None else 1800.0
        return value if math.isfinite(value) and value > 0 else 1800.0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1800.0


def _barrier_stall(
    track: object, stage: object, route: dict, now: float | None = None
) -> tuple[bool, float | None]:
    members = _enforceable_barrier_members(track, stage)
    if not members:
        return False, None
    keys = [track.member_key(stage.stage_id, member.member_id) for member in members]
    # A member that already failed keeps none of the three request markers: the
    # failure paths drop it from requested, member_tasks and requested_at. Demanding
    # them for every member would disable the only automatic barrier escape exactly
    # in the case that needs it, so failed members count as stalled on their own.
    pending = [key for key in keys if key not in track.failed]
    stamps: list[float] = []
    for key in pending:
        member = track.member_for_key(key)
        unavailable = member is not None and _role_signal_unverified(member.role)
        if unavailable:
            # Availability loss is itself the request to wait: never-spawned
            # unavailable members must not prevent the bounded stall clock.
            stamps.append(track.requested_at.get(key, track.updated_at))
        elif key in track.requested and key in track.member_tasks:
            stamps.append(track.requested_at.get(key, track.updated_at))
        else:
            return False, None
    now = time.time() if now is None else now
    age = now - (min(stamps) if stamps else track.updated_at)
    return age > _barrier_stall_seconds(route), age


def _load_execution(decision_id: str | None):
    if not decision_id:
        return None
    return load_state(default_state_path()).get_execution(decision_id)


def _ensure_track(route: dict, decision_id: str) -> None:
    ensure_execution_tx(
        default_state_path(),
        decision_id,
        route.get("session_id"),
        route.get("turn_id"),
        route.get("execution") or [],
    )


def _route_has_required_stage(route: dict) -> bool:
    for stage in route.get("execution") or []:
        if isinstance(stage, dict) and stage.get("required"):
            return True
    return False


def _pipeline_unfinished(decision_id: str | None, route: dict) -> bool:
    """True when the route still has an unfinished required execution stage.

    The gate is driven by the executor track, independent of model identity:
    a same-model parent must still walk the pipeline before editing.
    A missing track (missed submit / first PreToolUse) gates iff the route
    itself declares at least one required stage; `_ensure_track` materialises
    it immediately afterwards.
    """
    if not decision_id:
        return False
    _ensure_track(route, decision_id)
    track = _load_execution(decision_id)
    if track is None:
        return _route_has_required_stage(route)
    return not track.all_required_completed()


def _role_signal_unverified(role: str) -> bool:
    """True when registry metadata requires a missing live-session auth signal."""
    return not _role_availability_verified(role)


def _enforceable_barrier_members(track: object, stage: object) -> list:
    """Return every pending required member; unavailable members still stall."""
    return list(track.incomplete_required_members(stage))


def _next_required_spawn_stage(track: object):
    """Return the current enforceable linear stage or parallel barrier."""
    ordered = getattr(track, "next_incomplete_required_stage", lambda: None)()
    if ordered is not None and not ordered.spawnable:
        return None
    sentinel = next(
        (
            stage
            for stage in getattr(track, "stages", ())
            if stage.required and stage.kind == "sentinel"
        ),
        None,
    )
    if sentinel is not None:
        return sentinel
    for stage in getattr(track, "stages", ()):
        if not stage.required or not stage.spawnable:
            continue
        if stage.kind == "parallel_spawn":
            if _enforceable_barrier_members(track, stage):
                return stage
            continue
        if stage.role in track.completed or track.review_inconclusive(stage.role):
            continue
        if stage.kind == "sentinel":
            return stage
        if _role_signal_unverified(stage.role):
            continue  # graceful observe-only
        return stage
    return None


def _next_required_spawn_role(track: object) -> str | None:
    stage = _next_required_spawn_stage(track)
    return stage.role if stage is not None and stage.kind != "parallel_spawn" else None


def _is_recon_stage(track: object, stage: object | None) -> bool:
    """True when the current required spawn is read-only repository recon."""
    if stage is None:
        return False
    if stage.kind == "parallel_spawn":
        members = _enforceable_barrier_members(track, stage)
        return bool(members) and all(member.role in roles.RECON_ROLES for member in members)
    return stage.role in roles.RECON_ROLES


def _enforceable_gate(decision_id: str | None, route: dict) -> tuple[bool, str]:
    """Return (lifted, detail).

    `lifted=True` means the gate should NOT hold the conductor right now.
    A stage whose registry role requires a missing live-session signal degrades
    to graceful observe-only instead of an impossible hard gate; a pending
    deterministic verification step produces a verify recipe rather than a
    spawn recipe.
    """
    track = _load_execution(decision_id)
    if track is None:
        return True, ""
    route_warnings = route.setdefault("warnings", [])
    for warning in track.warnings:
        if warning not in route_warnings:
            route_warnings.append(warning)
    next_stage = _next_required_spawn_stage(track)
    if next_stage is not None and next_stage.kind == "sentinel":
        return False, next_stage.reason
    next_verify = None
    if next_stage is None:
        for step in track.required_verify_steps():
            if step not in track.verified:
                next_verify = step
                break
    if next_stage is None and next_verify is None:
        return True, ""
    if next_verify is not None and next_stage is None:
        return False, _verify_recipe(route, next_verify, track)
    if next_stage is not None:
        if next_stage.kind == "parallel_spawn":
            stalled, age = _barrier_stall(track, next_stage, route)
            if stalled:
                warnings = route.setdefault("warnings", [])
                if "barrier_stall" not in warnings:
                    warnings.append("barrier_stall")
                return (
                    True,
                    f"barrier_stall: barrier {next_stage.stage_id} stalled for {age:.0f}s; spawn retries still require normal member validation",
                )
            return False, _barrier_recipe(route, track, next_stage)
        if next_stage.kind in {"judge", "evidence_collection"}:
            return False, _control_plane_recipe(route, next_stage)
        return False, _stage_recipe(route, next_stage.role, track)
    return False, _hold_recipe(route)


def _control_plane_recipe(route: dict, stage: object) -> str:
    """Recipe for the two control-plane stage kinds.

    A judge compares sanitized artifacts and must never be handed the raw
    responses or the models behind them; an evidence-collection stage runs the
    experiment a judge asked for and must not decide the comparison itself.
    Saying so in the recipe is what keeps the two roles from merging.
    """
    pair = _format_pair(stage.role)
    if stage.kind == "judge":
        duty = (
            "compare the sanitized candidate bundles against the task contract and the "
            "verification evidence. You see artifacts only: no model, provider or author "
            "identity, no raw response and no candidate's own explanation. Report which "
            "candidate the evidence supports, or that you cannot tell - never guess, and "
            "never let a stylistic preference outweigh a deterministic failure"
        )
    else:
        duty = (
            "run the experiment the verifier-planner asked for and report its outcome per "
            "candidate. Do not decide the comparison: obtaining the evidence and judging it "
            "are separate stages on purpose"
        )
    return (
        f'route={route.get("intent")}: NEXT: spawn subagent_type="{stage.role}" ({pair}) to {duty}. '
        f"The conductor remains permanently zero-write; this stage is read-only. "
        f"Retrieve the terminal result by id; an acknowledgement does not complete the stage. "
        f"Retrieve task results one id at a time. {STOP_FEEDBACK_MARK}."
    )


def _reactive_hint(track: object, key: str) -> str:
    marker = getattr(track, "reactive_respawns", {}).get(key)
    if not isinstance(marker, dict) or int(marker.get("count", 0)) < 1:
        return ""
    reserve_hold = marker.get("unavailable_until")
    if reserve_hold is not None:
        try:
            reserve_hold = float(reserve_hold)
        except (TypeError, ValueError):
            return ""
        if reserve_hold <= time.time():
            return ""
    return (
        f"primary {marker.get('from_provider')} quota-held; respawn {marker.get('role', key)} "
        f"— routes to reserve {marker.get('to_provider')}. "
    )


def _bound_task_suffix(track: object | None, role: str) -> str:
    if track is not None and getattr(track, "stage_tasks", {}).get(role):
        task_id = track.stage_tasks[role]
        return f" Retrieve task {task_id} results one id at a time to settle this stage."
    return ""


def _stage_recipe(route: dict, role: str, track: object | None = None) -> str:
    pair = _format_pair(role)
    reactive = _reactive_hint(track, role) if track is not None else ""
    if role in roles.RECON_ROLES:
        return (
            f'route={route.get("intent")}: NEXT: {reactive}spawn subagent_type="{role}" ({pair}) with a tight read-only recon prompt. '
            f"Stage Gating: normal lifecycle coordination; next required stage is '{role}'. The conductor remains permanently zero-write; use read_file only for prompt context. "
            f"Retrieve the terminal result by id; an acknowledgement does not complete the stage. "
            f"Retrieve task results one id at a time.{_bound_task_suffix(track, role)} {STOP_FEEDBACK_MARK}."
        )
    snippet = _prompt_snippet(route.get("session_id"))
    prompt = snippet or f"Do the {role} work from the last user turn."
    capability = ' with capability_mode="all"' if role in STATION_SKILLS["implement"] else ""
    return (
        f'route={route.get("intent")}: NEXT: {reactive}call spawn_subagent with subagent_type="{role}"{capability} '
        f'using {pair} and prompt "{prompt}". '
        f"The conductor remains permanently zero-write; only the spawned child may edit. "
        f"Retrieve task results one id at a time.{_bound_task_suffix(track, role)} {STOP_FEEDBACK_MARK}."
    )


def _barrier_recipe(route: dict, track: object, stage: object) -> str:
    members = _enforceable_barrier_members(track, stage)
    outstanding = ", ".join(
        f"{track.member_key(stage.stage_id, member.member_id)} -> {member.role} "
        f"({_format_pair(member.role)}; {_reactive_hint(track, track.member_key(stage.stage_id, member.member_id))}{member.reason})"
        for member in members
    )
    recon = all(member.role in roles.RECON_ROLES for member in members)
    awaiting = [
        (f"{key}={track.member_tasks[key]}" if key in track.member_tasks else key)
        for member in members
        for key in [track.member_key(stage.stage_id, member.member_id)]
        if key in track.requested
    ]
    spawnable = [
        f"{track.member_key(stage.stage_id, member.member_id)} -> {member.role}"
        for member in members
        for key in [track.member_key(stage.stage_id, member.member_id)]
        if key not in track.requested or key not in track.member_tasks
    ]
    diet = " Do not grep or list the tree on the conductor." if recon else ""
    capability = (
        ' Implementation members must be spawned with capability_mode="all".'
        if any(member.role in STATION_SKILLS["implement"] for member in members)
        else ""
    )
    action = (
        f"spawn retryable members: {', '.join(spawnable)}"
        if spawnable
        else f"retrieve bound task ids: {', '.join(awaiting) or 'none'}"
    )
    consilium = (
        " Consilium: three cross-provider read-only diagnosticians; spawn all members, retrieve their verdicts, then have an implementation tier apply the arbiter plan."
        if stage.stage_id == "consilium"
        else ""
    )
    failure_reasons = getattr(track, "failure_reasons", {})
    reasons = " ".join(
        f"{key} failed: {failure_reasons[key]} (binding released after two unresolved registry lookups; member is retryable)."
        for key in failure_reasons
        if key in {track.member_key(stage.stage_id, member.member_id) for member in members}
    )
    reason_hint = f" {reasons}" if reasons else ""
    return (
        f"route={route.get('intent')}: NEXT: {action}. "
        f"Barrier '{stage.stage_id}' members: {outstanding}; bound tasks: {', '.join(awaiting) or 'none'}."
        f"{diet}{capability}{consilium} Retrieve bound task ids one id at a time (one at a time); TEXT-ONLY multi-id batch successes cannot settle members (structural per-ID successes are supported); if all statuses remain incomplete/not_found, this is a no-op, composition unchanged. "
        f"Retrieve task results one id at a time."
        f"{reason_hint} {STOP_FEEDBACK_MARK}."
    )


def _verify_recipe(route: dict, step: str, track: object) -> str:
    command = _verify_command_for(track, step)
    role = str(
        route.get("role")
        or ROLE_ALIASES.get(str(route.get("intent") or ""), route.get("intent") or "")
    )
    pair = _format_pair(role)
    if not command:
        return (
            f"route={route.get('intent')}: NEXT: diagnose the missing deterministic verifier '{step}' without editing. "
            f"The configured route pair is {pair}; the conductor remains zero-write. {STOP_FEEDBACK_MARK}."
        )
    return (
        f'route={route.get("intent")}: NEXT: run the exact verifier with run_terminal_command command "{command}". '
        f"The configured route pair is {pair}; no direct-write command is allowed. {STOP_FEEDBACK_MARK}."
    )


def _hold_recipe(route: dict) -> str:
    role = str(
        route.get("role")
        or ROLE_ALIASES.get(str(route.get("intent") or ""), route.get("intent") or "")
    )
    pair = _format_pair(role)
    return (
        f"route={route.get('intent')}: NEXT: re-run the failed child stage before continuing "
        f"(configured route pair: {pair}). The conductor remains permanently zero-write. {STOP_FEEDBACK_MARK}."
    )


def _declarative_gated(route: dict, spec: dict | None = None) -> bool:
    """True when intent policy marks this route as edit-gated."""
    intent = str(route.get("intent") or "")
    if "block_tools" in route:
        return bool(route.get("block_tools"))
    policy_spec = spec if spec is not None else load_intents()
    return intent in gated_intents(policy_spec)


def _gate_strong(route: dict) -> bool:
    """True when strong evidence or an intent-specific phrase gate applies."""
    has_strong = bool(route.get("has_strong"))
    intent = str(route.get("intent") or "")
    profile_name = str(route.get("profile") or "default")
    try:
        profiles = load_profiles()
    except (OSError, json.JSONDecodeError):
        profiles = {}
    profile = profiles.get(profile_name)
    require_strong = profile.require_strong_for_gate if profile is not None else True
    phrase_intents = profile.gate_phrase_intents if profile is not None else ()
    return has_strong or intent in phrase_intents or not require_strong


def _gate_active(route: dict, spec: dict) -> bool:
    """True for a strong edit-gated route with unfinished required execution."""
    return (
        (route.get("mode") or "dynamic") == "dynamic"
        and route.get("role_spawnable", True)
        and route.get("execution_valid", True)
        and _declarative_gated(route, spec)
        and _gate_strong(route)
        and _pipeline_unfinished(route.get("decision_id"), route)
    )


EDIT_TOOLS = frozenset(
    {
        "search_replace",
        "write",
        "edit",
        "multiedit",
        "strreplace",
        "run_terminal_command",
        "workflow",
        "image_edit",
        "image_gen",
        "image_to_video",
        "reference_to_video",
    }
)
