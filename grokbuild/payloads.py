"""Parse task-tool payloads without depending on the live route hook.

SHAPE TABLE
===========
``spawn_subagent``
    Parsed: plain foreground result text; ``{"type": "Text", "text": ...}``;
    exact background acknowledgements in either multiline form
    ``Subagent started in background.\nsubagent_id: UUID`` or single-line form
    ``subagent_id: UUID``. The exact acknowledgement regex is intentionally
    narrow. Other fields on observed spawn payloads are not interpreted.
``run_terminal_command``
    Observed: background acknowledgement
    ``{"type": "BackgroundTaskStarted", ...}``. Its documented identifier
    dictionary fields are parsed; this shape is not treated as terminal output.
``get_command_or_subagent_output``
    Parsed: ``{"type": "TaskOutput", "Result": {...}}`` retrieval envelopes,
    including terminal status, exit code, output, and batch-section synthesis.
Legacy results
    Parsed: plain strings; dictionaries containing ``content``, ``result``,
    ``text``, ``summary``, or ``output``; and text content-block lists.
Transcript fallback
    Parsed only when a requested task id exists and the direct result is empty.
    The newest 200 records of ``<session>/chat_history.jsonl`` are searched by
    matching payload ``toolUseId``/``tool_use_id`` to transcript
    ``tool_call_id``/``toolCallId``; string or text-block-list content is used.
    Session lookup and reverse-record iteration are injected by the caller.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable

from grokbuild.persist import append_jsonl

_SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,72}")
_FOREGROUND_SUBAGENT_META_RE = re.compile(
    r"<subagent_meta>id=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}),[ \t]*type=([a-z][a-z0-9-]*)</subagent_meta>"
)
_TERMINAL_TASK_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_-]+)</task-id>")
INCOMPLETE_MARKERS = ("started", "running", "in progress", "launched", "queued")
COMPLETION_SIGNALS = re.compile(
    r"\b(?:complete|completed|passed|finished|done|found|fixed|verified|summary|report)\b",
    re.IGNORECASE,
)
_ERROR_RESULTS = frozenset(
    {
        "error",
        "failed",
        "failure",
        "timeout",
        "timed out",
        "cancelled",
        "canceled",
        "aborted",
        "exception",
        "task failed",
        "build failed",
        "operation failed",
        "execution failed",
        "run failed",
        "spawn failed",
        "command failed",
        "process failed",
        "failed to apply",
        "failed to complete",
        "unsuccessful",
        "did not complete",
        "could not complete",
    }
)
_ERROR_PREFIXES = (
    "error:",
    "error ",
    "failed:",
    "failed ",
    "failure:",
    "failure ",
    "traceback",
    "exception:",
    "exception ",
    "timed out",
    "timeout:",
    "cancelled",
    "canceled",
    "aborted",
    "task failed",
    "build failed",
    "operation failed",
    "execution failed",
    "run failed",
    "spawn failed",
    "command failed",
    "process failed",
    "unsuccessful",
    "did not complete",
    "could not complete",
    "unable to apply",
    "unable to complete",
    "no such file",
    "permission denied",
    "connection refused",
)
_INABILITY_RESULT_PHRASES = (
    "i could not",
    "could not execute",
    "unable to execute",
    "couldn't run",
    "cannot complete",
    "cannot execute",
    "can't complete",
    "can't execute",
)
_RETRIEVAL_TASK_HEADER_RE = re.compile(
    r"^(?:---\s*Task\s+(.+?)\s+\[([^\]]+)\]\s*---|===\s*Task\s+([^=]+?)\s*===)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RETRIEVAL_TASK_NOT_FOUND_RE = re.compile(
    r"\bTask\s+([A-Za-z0-9_-]+)\s+not\s+found\.?",
    re.IGNORECASE,
)
_REGISTRY_EMPTY_RE = re.compile(
    r"\bNo\s+background\s+tasks\s+or\s+subagents\s+exist\s+in\s+this\s+session\.?",
    re.IGNORECASE,
)
# A retrieval section names the child it reports on: ``Command: [subagent:<type>]``
# for the spawn line, ``Type: <type>`` in the progress block of a running child.
_RETRIEVAL_SECTION_ROLE_RE = re.compile(
    r"^(?:Command:\s*\[subagent:([a-z][a-z0-9-]*)\]|Type:\s*([a-z][a-z0-9-]*)\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
# Terminal failure lines a dead child leaves in its result body. ``Exit Code``
# is authoritative only when non-zero; ``Session error`` is always terminal.
_FAILURE_LINE_RE = re.compile(
    r"^(?:Exit Code:\s*(?!0\s*$)-?\d+\s*$|Session error:)", re.IGNORECASE | re.MULTILINE
)
_STRUCTURED_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "timeout",
        "timed_out",
        "aborted",
    }
)
_STRUCTURED_INCOMPLETE_STATUSES = frozenset(
    {"running", "queued", "pending", "in_progress", "not_started", "incomplete", "started"}
)

TranscriptCallback = Callable[[dict], str | None]


def says_subagent(data: dict) -> bool:
    # Top-level host-injected marker only; never the toolInput request field.
    return bool(str(data.get("subagentType") or "").strip())


def tool_input(data: dict) -> dict:
    value = data.get("toolInput") or data.get("tool_input") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def spawn_tool_input(data: dict) -> dict:
    value = tool_input(data)
    nested = value.get("tool_input")
    return nested if isinstance(nested, dict) else value


def background_ack_id(raw: object) -> str | None:
    """Return an id only from the documented background-subagent acknowledgement."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    match = re.match(
        r"\ASubagent started in background\.[ \t]*\r?\nsubagent_id:[ \t]*([0-9a-fA-F-]{8,72})[ \t]*(?:\r?\n|$)",
        raw,
    )
    if not match:
        match = re.fullmatch(r"subagent_id:[ \t]*([0-9a-fA-F-]{8,72})[ \t]*", raw)
    return match.group(1) if match and _SESSION_ID_RE.fullmatch(match.group(1)) else None


def _foreground_subagent_id(raw: str) -> str | None:
    """Extract metadata only from the final metadata line of a foreground result."""
    meta_lines = [line.strip() for line in raw.splitlines() if "<subagent_meta>" in line]
    if not meta_lines:
        return None
    match = _FOREGROUND_SUBAGENT_META_RE.fullmatch(meta_lines[-1])
    return match.group(1) if match else None


def task_ids(
    data: dict, include_result: bool = False, include_free_text_task_ids: bool = True
) -> list[str]:
    """Extract documented task/subagent ids and, optionally, terminal text ids."""
    sources = [tool_input(data)]
    raw = tool_result_raw(data) if include_result else None
    if isinstance(raw, dict):
        sources.append(raw)
    found: list[str] = []
    for source in sources:
        for key in ("task_id", "id", "subagent_id"):
            value = source.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                found.append(str(value))
        values = source.get("task_ids")
        if isinstance(values, list):
            found.extend(
                str(value)
                for value in values
                if isinstance(value, (str, int)) and not isinstance(value, bool)
            )
    ack_values = [raw] if isinstance(raw, str) else []
    if isinstance(raw, dict):
        ack_values.extend(
            raw.get(key)
            for key in ("content", "result", "text", "summary", "output")
            if isinstance(raw.get(key), str)
        )
    for value in ack_values:
        ack_id = background_ack_id(value)
        if ack_id:
            found.append(ack_id)
        if include_result and include_free_text_task_ids:
            found.extend(_TERMINAL_TASK_ID_RE.findall(value))
            if isinstance(raw, str):
                foreground_id = _foreground_subagent_id(value)
                if foreground_id:
                    found.append(foreground_id)
    return list(dict.fromkeys(found))


def terminal_background_ack(data: dict) -> bool:
    """True only for a background terminal acknowledgement."""
    raw = tool_result_raw(data)
    if isinstance(raw, dict):
        return raw.get("type") == "BackgroundTaskStarted" and bool(
            task_ids(data, include_result=True)
        )
    return False


def spawn_background(data: dict) -> bool:
    """True when a spawn request asked for a background subagent."""
    value = tool_input(data)
    background = value.get("background")
    if isinstance(background, bool):
        return background
    if isinstance(background, (int, float)):
        return bool(background)
    if isinstance(background, str):
        return background.strip().casefold() in {"1", "true", "yes", "on"}
    return False


def tool_result_raw(data: dict):
    """Return the raw tool result payload (``None`` when absent)."""
    for key in ("toolResult", "tool_result", "toolResponse", "tool_response"):
        value = data.get(key)
        if value is not None:
            if (
                isinstance(value, dict)
                and value.get("type") == "TaskOutput"
                and isinstance(value.get("Result"), dict)
            ):
                return value["Result"]
            return value
    return None


def looks_incomplete(content: str) -> bool:
    """Heuristic: does a spawn tool result look like an in-progress ack?"""
    if not isinstance(content, str):
        return False
    text = content.strip()
    if not text:
        return True
    low = text.casefold()
    if any(low.startswith(marker) for marker in INCOMPLETE_MARKERS):
        return True
    return any(marker in low for marker in INCOMPLETE_MARKERS) and not COMPLETION_SIGNALS.search(
        text
    )


def classify_result_text(text: str) -> str:
    """Classify a spawn result text as ``success`` | ``failure`` | ``incomplete``.

    A leading error phrase, an inability phrase, or a terminal failure line
    anywhere in the body (``Exit Code: <non-zero>``, ``Session error:``) is a
    failure; the last two are what a child killed by its provider leaves behind
    after a prose header, so they cannot be required to come first.
    """
    stripped = text.strip()
    if not stripped:
        return "incomplete"
    low = stripped.casefold()
    if low in _ERROR_RESULTS or low.startswith(_ERROR_PREFIXES):
        return "failure"
    if any(phrase in low for phrase in _INABILITY_RESULT_PHRASES):
        return "failure"
    if _FAILURE_LINE_RE.search(stripped):
        return "failure"
    if looks_incomplete(stripped):
        return "incomplete"
    return "success"


def structural_spawn_status(raw: object) -> str | None:
    """Classify a dictionary spawn result from its own status fields.

    ``exit_code``/``status``/``error`` are the harness's word, not the child's
    prose: a non-zero exit, a terminal failure status, or an error payload is a
    failure and a running status is incomplete regardless of any text. Fields
    that do not settle the question return ``None`` so the text decides.
    """
    if not isinstance(raw, dict):
        return None
    exit_code = raw.get("exit_code", raw.get("exitCode"))
    if isinstance(exit_code, str):
        try:
            exit_code = int(exit_code.strip())
        except ValueError:
            exit_code = None
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return "failure"
    status = raw.get("status")
    if isinstance(status, str):
        value = status.strip().casefold().replace(" ", "_")
        if value in _STRUCTURED_FAILURE_STATUSES:
            return "failure"
        if value in _STRUCTURED_INCOMPLETE_STATUSES:
            return "incomplete"
    elif isinstance(status, int) and not isinstance(status, bool) and status != 0:
        return "failure"
    error = raw.get("error")
    if isinstance(error, str) and error.strip():
        return "failure"
    if isinstance(error, dict) and error:
        return "failure"
    return None


def spawn_result_status(data: dict) -> str:
    """Return the authoritative spawn result classification."""
    if spawn_background(data):
        return "incomplete"
    raw = tool_result_raw(data)
    if raw is None:
        return "incomplete"
    if isinstance(raw, str):
        return classify_result_text(raw)
    if isinstance(raw, dict):
        structural = structural_spawn_status(raw)
        if structural is not None:
            return structural
        text = (
            raw.get("content")
            or raw.get("result")
            or raw.get("text")
            or raw.get("summary")
            or raw.get("output")
        )
        if isinstance(text, str):
            return classify_result_text(text)
        return "incomplete"
    return "incomplete"


def spawn_result_ok(data: dict) -> bool:
    """True only for a definitive, finished-looking spawn success."""
    return spawn_result_status(data) == "success"


def content_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts) if parts else None
    return None


def _envelope_header_lines(raw: dict) -> list[str] | None:
    """Authoritative status lines for a TaskOutput.Result envelope, without output."""
    task_id = raw.get("task_id") or raw.get("taskId")
    if not task_id:
        return None
    status = raw.get("status")
    status_value = status.strip().casefold() if isinstance(status, str) else ""
    exit_code = raw.get("exit_code", raw.get("exitCode"))
    definitive_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    terminal_statuses = {
        "completed",
        "complete",
        "success",
        "succeeded",
        "failed",
        "cancelled",
        "canceled",
        "error",
    }
    if status_value not in terminal_statuses and not definitive_exit:
        return [f"=== Task {task_id} ===", "Status: incomplete"]
    lines = [f"=== Task {task_id} ==="]
    if status_value:
        lines.append(f"Status: {status}")
    if definitive_exit:
        lines.append(f"Exit Code: {exit_code}")
    return lines


def structured_retrieval_text(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [text for item in raw if (text := structured_retrieval_text(item))]
        return "\n".join(parts) if parts else None
    if not isinstance(raw, dict):
        return None
    if raw.get("task_id") or raw.get("taskId"):
        lines = _envelope_header_lines(raw) or []
        output = content_text(raw.get("output"))
        if output:
            lines.extend(("", "=== Output ===", output))
        return "\n".join(lines)
    content = content_text(raw.get("content"))
    direct = next(
        (
            value
            for value in (content, raw.get("result"), raw.get("text"), raw.get("summary"))
            if isinstance(value, str)
        ),
        None,
    )
    if direct:
        return direct
    output = content_text(raw.get("output"))
    return output if output else None


def retrieval_transcript_text(
    data: dict,
    session_dir: Callable[[str | None], Path | None],
    session_id: Callable[[dict], str | None],
    iter_jsonl_reversed: Callable[[Path], Iterable[dict]],
) -> str | None:
    tool_use_id = str(data.get("toolUseId") or data.get("tool_use_id") or "")
    folder = session_dir(session_id(data))
    if not tool_use_id or folder is None:
        return None
    for index, record in enumerate(iter_jsonl_reversed(folder / "chat_history.jsonl")):
        if index >= 200:
            break
        if record.get("type") != "tool_result":
            continue
        call_id = str(record.get("tool_call_id") or record.get("toolCallId") or "")
        if call_id == tool_use_id:
            return content_text(record.get("content"))
    return None


def retrieval_result_text(
    data: dict, transcript_callback: TranscriptCallback | None = None
) -> str | None:
    direct = structured_retrieval_text(tool_result_raw(data))
    if isinstance(direct, str) and direct.strip():
        return direct
    if not task_ids(data):
        return None
    return transcript_callback(data) if transcript_callback else None


def structural_task_statuses(raw: object) -> dict[str, str]:
    """Per-id statuses from TaskOutput.Result envelope fields only.

    Only the envelope's own task_id, status, and exit_code count; header-like
    text inside its output is never re-parsed here.
    """
    items = raw if isinstance(raw, list) else [raw]
    statuses: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        lines = _envelope_header_lines(item)
        if lines is None:
            continue
        task_id = str(item.get("task_id") or item.get("taskId"))
        statuses[task_id] = retrieval_section_status("\n".join(lines))
    return statuses


def retrieval_task_sections(raw: str | None) -> dict[str, str]:
    """Map each task id in a retrieval text to its own section, header included."""
    if not isinstance(raw, str):
        return {}
    headers = list(_RETRIEVAL_TASK_HEADER_RE.finditer(raw))
    sections: dict[str, str] = {}
    for index, header in enumerate(headers):
        task_id = (header.group(1) or header.group(3)).strip()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(raw)
        sections[task_id] = raw[header.start() : end]
    return sections


def retrieval_section_role(section: str) -> str:
    """Return the subagent type a retrieval section names, or ``""``."""
    match = _RETRIEVAL_SECTION_ROLE_RE.search(section or "")
    if match is None:
        return ""
    return str(match.group(1) or match.group(2) or "").casefold()


def retrieval_task_statuses(
    data: dict,
    transcript_callback: TranscriptCallback | None = None,
    requested_ids: list[str] | None = None,
) -> dict[str, str]:
    """Map retrieval section statuses by task id.

    In a multi-id batch a child cannot crown a sibling: text-header ``success``
    clamps to ``incomplete`` and only the structural TaskOutput.Result envelope
    for that id stays authoritative for success.
    """
    multi = requested_ids is not None and len(requested_ids) > 1
    raw = retrieval_result_text(data, transcript_callback)
    statuses: dict[str, str] = {}
    if raw is not None:
        headers = list(_RETRIEVAL_TASK_HEADER_RE.finditer(raw))
        for index, header in enumerate(headers):
            task_id = (header.group(1) or header.group(3)).strip()
            bracket_status = (header.group(2) or "").strip().casefold()
            end = headers[index + 1].start() if index + 1 < len(headers) else len(raw)
            status = retrieval_section_status(raw[header.end() : end])
            if bracket_status in {"not_found", "not found"}:
                status = "not_found"
            elif bracket_status in {"running", "queued"}:
                status = "incomplete"
            elif bracket_status in {"failed", "cancelled", "canceled", "error"}:
                status = "failure"
            elif (
                bracket_status in {"completed", "complete", "success", "succeeded"}
                and status != "failure"
            ):
                status = "success"
            if multi and status == "success":
                status = "incomplete"
            rank = {"failure": 0, "not_found": 1, "incomplete": 2, "success": 3}
            if task_id not in statuses or rank.get(status, 2) < rank.get(statuses[task_id], 2):
                statuses[task_id] = status
        for match in _RETRIEVAL_TASK_NOT_FOUND_RE.finditer(raw):
            statuses[match.group(1)] = "not_found"
        if _REGISTRY_EMPTY_RE.search(raw):
            for task_id in requested_ids or ():
                statuses.setdefault(task_id, "not_found")
    for task_id, status in structural_task_statuses(tool_result_raw(data)).items():
        if statuses.get(task_id) != "not_found":
            statuses[task_id] = status
    return statuses


def retrieval_result_status(
    data: dict,
    transcript_callback: TranscriptCallback | None = None,
    requested_ids: list[str] | None = None,
) -> str:
    """Retrieval completes a member only from terminal non-empty plain text."""
    raw = retrieval_result_text(data, transcript_callback)
    if not isinstance(raw, str):
        return "incomplete"
    low = raw.strip().casefold()
    if not low or low in {"timeout", "timed out"} or low.startswith(("timeout:", "timed out")):
        return "incomplete"
    statuses = retrieval_task_statuses(data, transcript_callback, requested_ids)
    task_statuses = [status for status in statuses.values() if status != "not_found"]
    if task_statuses:
        if "incomplete" in task_statuses:
            return "incomplete"
        if "failure" in task_statuses:
            return "failure"
        return "success"
    if statuses:
        return "incomplete"
    return retrieval_section_status(raw)


def retrieval_section_status(raw: str) -> str:
    status_match = re.search(r"^Status:\s*([^\r\n]+)", raw, re.IGNORECASE | re.MULTILINE)
    exit_match = re.search(r"^Exit Code:\s*(-?\d+)", raw, re.IGNORECASE | re.MULTILINE)
    status_value = status_match.group(1).strip().casefold() if status_match else ""
    if status_value in {"failed", "cancelled", "canceled", "error"}:
        return "failure"
    if exit_match and int(exit_match.group(1)) != 0:
        return "failure"
    if status_value in {
        "running",
        "queued",
        "pending",
        "in_progress",
        "not_started",
        "incomplete",
    } or raw.strip().casefold().startswith(("timeout:", "timed out")):
        return "incomplete"
    if status_value in {"completed", "complete", "success", "succeeded"}:
        return "success"
    if exit_match:
        return "success"
    if status_match:
        return "incomplete"
    if re.search(r"^===\s*Task\s+[^=]+===\s*$", raw, re.IGNORECASE | re.MULTILINE):
        return "success"
    return classify_result_text(raw)


def _payload_shape(value: object, *, tool_result: bool = False) -> object:
    """Return types and lengths without retaining payload values."""
    if isinstance(value, dict):
        if tool_result:
            return {
                "type": "object",
                "keys": {
                    str(key): {"type": type(item).__name__, "length": _value_length(item)}
                    for key, item in value.items()
                },
            }
        return {
            "type": "object",
            "keys": {
                str(key): _payload_shape(item, tool_result=str(key) == "toolResult")
                for key, item in value.items()
            },
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": type(value).__name__,
            "length": len(value),
            "items": [_payload_shape(item) for item in value],
        }
    return {"type": type(value).__name__, "length": _value_length(value)}


def _value_length(value: object) -> int | None:
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return None


def dump_payload_debug(data: dict, state_directory: Path, timestamp: str) -> None:
    """Append a structure-only payload capture when explicitly enabled."""
    if not (state_directory / "payload-debug.enabled").is_file():
        return
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        # The shared appender brings the per-file lock, 0600 mode and the same
        # size rotation every other sidecar gets; a raw O_APPEND here was the one
        # unbounded writer in the state directory.
        append_jsonl(
            state_directory / "payload-debug.jsonl",
            {"observed_at": timestamp, "payload": _payload_shape(data)},
        )
    except OSError:
        pass


__all__ = [
    "background_ack_id",
    "classify_result_text",
    "content_text",
    "dump_payload_debug",
    "looks_incomplete",
    "retrieval_result_status",
    "retrieval_result_text",
    "retrieval_section_role",
    "retrieval_section_status",
    "retrieval_task_sections",
    "retrieval_task_statuses",
    "retrieval_transcript_text",
    "spawn_background",
    "spawn_result_ok",
    "spawn_result_status",
    "structural_spawn_status",
    "structural_task_statuses",
    "structured_retrieval_text",
    "task_ids",
    "tool_input",
    "tool_result_raw",
]
