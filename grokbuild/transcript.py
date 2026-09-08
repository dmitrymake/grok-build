"""Session and transcript tracking helpers."""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

from grokbuild.classify import extract_user_text, is_synthetic_stop_feedback
from grokbuild.payloads import looks_incomplete
from grokbuild.persist import atomic_update_json, sidecar_path
from grokbuild.roles import IMPLEMENT_ROLE_NAMES, ROLE_ALIASES, load_registry
from grokbuild.state import default_state_path, load_state

SPAWN_TOOLS = frozenset({"spawn_subagent", "task"})

STATION_SKILLS = {
    "security": frozenset({"security", "security-verify"}),
    "implement": IMPLEMENT_ROLE_NAMES,
    "review": frozenset({"review", "review-hard"}),
    "plan": frozenset({"plan", "plan-hard"}),
    "explore": frozenset({"explore", "explore-thorough"}),
}

TAIL_BYTES = 256_000

TURN_CACHE_LIMIT = 32

_TURN_COUNT_CACHE: OrderedDict[tuple[Path, int, int], int] = OrderedDict()
_SESSION_KIND_CACHE: tuple[int, dict[str, dict[str, object]]] | None = None


def _session_kind_pins() -> dict[str, dict[str, object]]:
    global _SESSION_KIND_CACHE
    path = sidecar_path("session-kinds.json")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    if _SESSION_KIND_CACHE is not None and _SESSION_KIND_CACHE[0] == mtime:
        return _SESSION_KIND_CACHE[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    pins = data if isinstance(data, dict) else {}
    _SESSION_KIND_CACHE = (mtime, pins)
    return pins


def _pin_session_kind(session_id: str, kind: str) -> None:
    global _SESSION_KIND_CACHE
    path = sidecar_path("session-kinds.json")
    now = time.time()

    def update(data):
        pins = dict(data) if isinstance(data, dict) else {}
        pins[session_id] = {"kind": kind, "seen_at": now}
        while len(pins) > 256:
            oldest = min(pins, key=lambda key: float(pins[key].get("seen_at", 0)))
            del pins[oldest]
        return pins

    atomic_update_json(path, update, default={})
    _SESSION_KIND_CACHE = None


SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,72}$")

PROMPT_KEYS = (
    "prompt",
    "text",
    "userPrompt",
    "user_prompt",
    "content",
    "message",
    "promptText",
)


def _grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))


def _walk_prompt(value: object, depth: int = 0) -> str | None:
    if depth > 4 or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in PROMPT_KEYS:
            if key in value:
                found = _walk_prompt(value[key], depth + 1)
                if found:
                    return found
        for nested in ("hookSpecificOutput", "input", "event"):
            if nested in value:
                found = _walk_prompt(value[nested], depth + 1)
                if found:
                    return found
    if isinstance(value, list):
        parts = [part for item in value if (part := _walk_prompt(item, depth + 1))]
        if parts:
            return "\n".join(parts)
    return None


def _session_dir(session_id: str | None) -> Path | None:
    if not session_id or not SESSION_ID_RE.fullmatch(session_id):
        return None
    sessions = _grok_home() / "sessions"
    if not sessions.is_dir():
        return None
    try:
        root = sessions.resolve()
    except OSError:
        return None
    matches: list[Path] = []
    for path in sessions.glob(f"*/{session_id}"):
        if not path.is_dir() or path.name != session_id:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(root) and (path / "summary.json").is_file():
            matches.append(path)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    ranked: list[tuple[float, Path]] = []
    for path in matches:
        try:
            ranked.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def _current_model(session_id: str | None) -> str | None:
    folder = _session_dir(session_id)
    if folder is None:
        return None
    summary = folder / "summary.json"
    if not summary.is_file():
        return None
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    model = data.get("current_model_id")
    return str(model) if model else None


def ensure_subagent_pin(session_id: str | None) -> None:
    """Promote a session pin through the trusted host-payload path only."""
    if not session_id:
        return
    pinned = _session_kind_pins().get(session_id)
    if isinstance(pinned, dict) and pinned.get("kind") == "subagent":
        return
    _pin_session_kind(session_id, "subagent")


def _is_subagent(session_id: str | None) -> bool:
    """True when this hook event belongs to a spawned station, not the conductor.

    A station (implement/review/security child) must be allowed to edit — the
    routing gate only ever holds the conductor. Grok marks such sessions with
    `session_kind` "subagent" (or "subagent_resume").
    """
    if not session_id:
        return False
    pinned = _session_kind_pins().get(session_id)
    if isinstance(pinned, dict) and pinned.get("kind"):
        return pinned["kind"] == "subagent"
    folder = _session_dir(session_id)
    if folder is None:
        return False
    summary = folder / "summary.json"
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    raw_kind = data.get("session_kind")
    if raw_kind is None and isinstance(data.get("info"), dict):
        raw_kind = data["info"].get("session_kind")
    kind = "subagent" if str(raw_kind or "") in {"subagent", "subagent_resume"} else "conductor"
    _pin_session_kind(session_id, kind)
    return kind == "subagent"


def _iter_jsonl_reversed(path: Path, chunk_size: int = TAIL_BYTES):
    """Yield JSON objects from the end of a JSONL file, chunk by chunk."""
    if not path.is_file():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    leftover = b""
    try:
        with path.open("rb") as handle:
            pos = size
            while pos > 0:
                read = min(chunk_size, pos)
                pos -= read
                handle.seek(pos)
                chunk = handle.read(read) + leftover
                parts = chunk.split(b"\n")
                leftover = parts[0]
                for raw in reversed(parts[1:]):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        yield item
            tail = leftover.strip()
            if tail:
                try:
                    item = json.loads(tail.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _iter_jsonl(path: Path):
    """Yield JSON objects forward from a JSONL file (used for turn counting)."""
    if not path.is_file():
        return
    try:
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return ""


def _is_real_user(record: dict) -> bool:
    if record.get("type") != "user":
        return False
    if record.get("synthetic_reason"):
        return False
    raw = _message_text(record)
    text = extract_user_text(raw)
    if not text:
        return False
    # Stop feedback is injected as a user turn. Match the complete generated
    # shape only, so quoted/embedded marker text cannot swallow a real turn.
    if is_synthetic_stop_feedback(text):
        return False
    return True


def last_user_prompt(session_id: str | None, history_path: Path | None = None) -> str:
    """Return the latest real user prompt from session history."""
    path = history_path
    if path is None:
        folder = _session_dir(session_id)
        if folder is None:
            return ""
        path = folder / "chat_history.jsonl"
    for record in _iter_jsonl_reversed(path):
        if _is_real_user(record):
            return extract_user_text(_message_text(record))
    return ""


def last_user_prompt_raw(session_id: str | None, history_path: Path | None = None) -> str:
    """Return the raw (unstripped) text of the latest real user turn.

    Synthetic turns, feedback injections, and pure harness reminders are still
    skipped by ``_is_real_user``; only the returned text is the raw message
    content rather than the ``extract_user_text``-stripped view, so the scorer
    can also see a fake mid-prompt reminder/info block.
    """
    path = history_path
    if path is None:
        folder = _session_dir(session_id)
        if folder is None:
            return ""
        path = folder / "chat_history.jsonl"
    for record in _iter_jsonl_reversed(path):
        if _is_real_user(record):
            return _message_text(record)
    return ""


def last_user_turn_number(session_id: str | None, history_path: Path | None = None) -> int | None:
    """Count real user turns in the transcript (1-based turn ordinal).

    This is the ground-truth turn id for hook events after the submit: it
    advances even when ``UserPromptSubmit`` was missed, so a repeated identical
    prompt in a new turn never collides with the previous turn's executor track.
    """
    path = history_path
    if path is None:
        folder = _session_dir(session_id)
        if folder is None:
            return None
        path = folder / "chat_history.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return 0
    key = (path, stat.st_size, stat.st_mtime_ns)
    cached = _TURN_COUNT_CACHE.get(key)
    if cached is not None:
        return cached
    count = 0
    for record in _iter_jsonl(path):
        if _is_real_user(record):
            count += 1
    _TURN_COUNT_CACHE[key] = count
    while len(_TURN_COUNT_CACHE) > TURN_CACHE_LIMIT:
        _TURN_COUNT_CACHE.popitem(last=False)
    return count


def _tool_calls(record: dict) -> list[dict]:
    calls = record.get("tool_calls") or record.get("toolCalls") or []
    return [call for call in calls if isinstance(call, dict)]


def _call_name(call: dict) -> str:
    return str(call.get("name") or call.get("toolName") or "").casefold()


def _call_args(call: dict) -> dict:
    raw = call.get("arguments") or call.get("input") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _wanted_station(intent: str | None) -> frozenset[str]:
    return STATION_SKILLS.get(intent or "", frozenset())


def _is_matching_station(child: str, intent: str | None) -> bool:
    wanted = _wanted_station(intent)
    return bool(wanted) and child.casefold() in wanted


def _canonical_role(name: str) -> str:
    return ROLE_ALIASES.get((name or "").casefold(), (name or "").casefold())


def turn_station_status(
    session_id: str | None,
    intent: str | None,
    history_path: Path | None = None,
    hook_data: dict | None = None,
) -> dict:
    """Return {"requested": bool, "result": bool|None} for this turn.

    `requested` = a matching `spawn_subagent` request was issued (or a matching
    background subagent is in flight). `result` is True only when a
    `tool_result` for that spawn call id carries a finished-looking summary.
    This proves a spawn *result was received*, never that the child verified or
    completed its work — a mere request is not a result. `result` is None when
    nothing was requested. Background subagents do not expose their final
    summary under the spawn call id, so their result stays False (unknown),
    never assumed.
    """
    hook_data = hook_data or {}
    requested = False
    result: bool | None = None
    spawn_ids: set[str] = set()

    for task in hook_data.get("backgroundTasks") or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("type") or "").casefold() != "subagent":
            continue
        agent = str(task.get("agentType") or task.get("agent_type") or "")
        if _is_matching_station(agent, intent):
            requested = True

    path = history_path
    if path is None:
        folder = _session_dir(session_id)
        if folder is None:
            return {"requested": requested, "result": False if requested else None}
        path = folder / "chat_history.jsonl"

    # Collect this turn's records (reverse iteration sees results before the
    # call), then replay them in forward order so spawn ids precede tool_results.
    turn_records: list[dict] = []
    for record in _iter_jsonl_reversed(path):
        if _is_real_user(record):
            break
        turn_records.append(record)
    for record in reversed(turn_records):
        rtype = record.get("type")
        if rtype == "assistant":
            for call in _tool_calls(record):
                name = _call_name(call)
                if name in SPAWN_TOOLS:
                    args = _call_args(call)
                    child = str(args.get("subagent_type") or args.get("subagentType") or "")
                    if _is_matching_station(child, intent):
                        requested = True
                        call_id = str(
                            call.get("id") or call.get("toolUseId") or call.get("tool_use_id") or ""
                        )
                        if call_id:
                            spawn_ids.add(call_id)
                elif name in {"enter_plan_mode"} and intent == "plan":
                    requested = True
        elif rtype == "tool_result":
            call_id = str(record.get("tool_call_id") or record.get("toolCallId") or "")
            if call_id and call_id in spawn_ids:
                if not looks_incomplete(record.get("content")):
                    result = True
    if result is None:
        result = False if requested else None
    return {"requested": requested, "result": result}


def _role_availability_verified(role_name: str, session_id: str | None = None) -> bool:
    """True unless registry metadata requires a missing live-session signal."""
    registry = load_registry()
    role = registry.get(role_name)
    if role is None:
        return False
    if not role.require_availability_signal:
        return True
    state = load_state(default_state_path())
    if state.stale or not state.is_available(role_name, session_id=session_id):
        return False
    if state.circuit_open(role_name, session_id=session_id):
        return False
    status = state.status_for(role_name)
    return bool(status.available) and bool(status.availability_signal)
