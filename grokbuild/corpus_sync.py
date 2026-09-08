#!/usr/bin/env python3
"""Harvest redacted routing outcomes into a manually adjudicated corpus."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import string
import subprocess
import sys
import time
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from grokbuild.classify import (
    extract_user_text,
    is_synthetic_stop_feedback,
    load_intents,
    normalize,
)
from grokbuild.persist import dump, parse_iso_utc, sidecar_path, state_dir, state_lock
from grokbuild.redact import redact_text
from grokbuild.roles import IMPLEMENT_ROLE_NAMES, ROLE_ALIASES, load_registry, resolve_config_path
from grokbuild.router import route_prompt
from ._repo import config_path

ROOT = Path(__file__).resolve().parent
OLD_STAGING = ROOT / "fixtures/workloads-staging.json"
DEFAULT_SESSIONS = Path.home() / ".grok/sessions"


_MARKER_FALLBACKS = {
    "automation_syntax": (
        "<task-notification>",
        "<command-name>",
        "<system-reminder>",
        "<user_query>",
        "[[GROK_ROUTE_STOP_FEEDBACK",
    ),
    "automation_prefixes": ("You are the ", "Работай в репозитории:"),
    "automation_brief": (
        "do not edit",
        "report back",
        "before any actions",
        "use a separate worktree",
        "return only",
        "independent review",
        "run the tests",
    ),
}


def _load_marker(name):
    try:
        data = (
            json.loads(
                (Path(__file__).resolve().parent / "intents.json").read_text(encoding="utf-8")
            )
            .get("markers", {})
            .get(name)
        )
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return tuple(data)
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return _MARKER_FALLBACKS[name]


_AUTOMATION_SYNTAX = _load_marker("automation_syntax")
_AUTOMATION_PREFIXES = _load_marker("automation_prefixes")
_AUTOMATION_BRIEF = _load_marker("automation_brief")

INTENTS = {"implement", "security", "review", "plan", "explore", None}
PREFIXES = {
    "data": ("analytics", "warehouse", "ingest", "etl"),
    "fw": ("firmware", "router", "flash", "image", "device"),
    "df": ("grok", "routing", "harness", "dotfiles", "hook"),
    "sec": ("cve", "пентест", "уязвим", "security", "утечк"),
    "game": ("game", "retention", "level", "monetization", "product"),
}


@functools.lru_cache(maxsize=8)
def _load_prefixes(path: Path | None):
    try:
        if path is None:
            raise ValueError
        table = (
            tomllib.loads(path.read_text(encoding="utf-8")).get("corpus", {}).get("taxonomy", {})
        )
        if (
            isinstance(table, dict)
            and table
            and all(
                isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
                for k, v in table.items()
            )
        ):
            return {k: tuple(v) for k, v in table.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return PREFIXES


def _prefixes():
    return _load_prefixes(resolve_config_path())


def _reset_prefixes_cache() -> None:
    _load_prefixes.cache_clear()


@functools.lru_cache(maxsize=1)
def _known_roles() -> set[str]:
    # Corpus validation accepts the historical registry surface, excluding internal-only roles.
    return (load_registry().names() | set(ROLE_ALIASES) | set(IMPLEMENT_ROLE_NAMES)) - {
        "consilium-analyst",
        "consilium-arbiter",
        "consilium-challenger",
        "explore-risk",
    }


def _reset_known_roles_cache() -> None:
    _known_roles.cache_clear()


PENDING_LIMIT = 200


def load(path: Path, default):
    """Load JSON from a path or return a default when the path is absent."""
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_sync_context(args, watermark_default=None):
    """Load corpus, staging, watermark, and shared deduplication state."""
    corpus = load(args.corpus, {"version": 1, "cases": []})
    staging = load(args.staging, [])
    watermark = load(args.watermark, watermark_default if watermark_default is not None else {})
    history = watermark.get("history") if isinstance(watermark.get("history"), dict) else {}
    hashes = [str(item) for item in history.get("prompt_hashes", [])]
    rejected = [str(item) for item in history.get("rejected_hashes", [])]
    previous = (
        history.get("staging_hashes", {})
        if isinstance(history.get("staging_hashes", {}), dict)
        else {}
    )
    rejected = _reconcile_rejected(staging, corpus.get("cases", []), previous, rejected)
    return corpus, staging, watermark, hashes, set(hashes), rejected


def _build_case(
    item_id,
    source,
    workspace,
    timestamp,
    prompt,
    origin,
    origin_rule,
    observed,
    evidence=None,
    flag=None,
):
    """Build the common history-import staging record."""
    return {
        "id": item_id,
        "harvested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "workspace": workspace,
        "timestamp": timestamp,
        "prompt": prompt[:400],
        "origin": origin,
        "origin_rule": origin_rule,
        "observed": observed,
        "git_changed": True if source == "git-history" else None,
        "git_evidence": evidence,
        "expect": None,
        "flag": flag,
        "notes": "",
    }


def migrate_staging(path: Path) -> None:
    """Merge the legacy staging file into the default staging path once."""
    if path != sidecar_path("workloads-staging.json") or not OLD_STAGING.exists():
        return
    current = load(path, []) if path.exists() else []
    legacy = load(OLD_STAGING, [])
    merged = []
    seen = set()
    for item in current + legacy:
        if not isinstance(item, dict) or item.get("id") in seen:
            continue
        seen.add(item.get("id"))
        merged.append(item)
    dump(path, merged, private_dir=state_dir())
    OLD_STAGING.unlink()


def text_of(record) -> str:
    """Extract text from a record content value."""
    content = record.get("content", record.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(x.get("text", ""))
            for x in content
            if isinstance(x, dict) and x.get("type") == "text"
        )
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text", ""))
    return ""


def real_users(session: Path):
    """Return non-synthetic user prompts from a session history."""
    try:
        records = [
            json.loads(line)
            for line in (session / "chat_history.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        return []
    result = []
    for record in records:
        if record.get("type") != "user" or record.get("synthetic_reason"):
            continue
        text = extract_user_text(text_of(record))
        if not text or is_synthetic_stop_feedback(text):
            continue
        result.append(text)
    return result


def session_for(root: Path, sid: str) -> Path | None:
    """Find the newest matching session directory under a root."""
    matches = [p for p in root.glob(f"*/{sid}") if p.is_dir()]
    return max(matches, key=lambda p: p.stat().st_mtime_ns) if matches else None


def norm(text: str) -> set[str]:
    """Normalize text into a set of comparable words."""
    text = normalize(text)
    text = text.translate(str.maketrans("", "", string.punctuation + "«»—–…"))
    return set(text.split())


def similarity(a: str, b: str) -> float:
    """Return the Jaccard similarity of two prompts."""
    aa, bb = norm(a), norm(b)
    return len(aa & bb) / len(aa | bb) if aa and bb else 0.0


def category(prompt: str) -> str:
    """Classify a prompt using configured taxonomy prefixes."""
    low = prompt.casefold()
    for prefix, words in _prefixes().items():
        if any(word in low for word in words):
            return prefix
    return "ms"


def git_check(workspace: str, observed_at: str):
    """Check whether a workspace has relevant Git activity or changes."""
    if not workspace:
        return None
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                workspace,
                "-c",
                "safe.directory=*",
                "log",
                "--oneline",
                f"--since={observed_at}",
                "--all",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", workspace, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if log.returncode or status.returncode:
            return None
        return bool(log.stdout.strip() or status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


def stage_ids(record):
    """Extract compact stage identifiers from an execution record."""
    execution = record.get("execution") or {}
    stages = execution.get("stages", []) if isinstance(execution, dict) else execution
    out = []
    for stage in stages if isinstance(stages, list) else []:
        if isinstance(stage, dict):
            out.append(
                {
                    k: stage.get(k)
                    for k in ("role", "kind", "stage_id", "id")
                    if stage.get(k) is not None
                }
            )
    return out


def workspace_for(summary: dict, slug: str) -> str:
    """Resolve a workspace path from a session summary and slug."""
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    return str(info.get("cwd") or summary.get("git_root_dir") or unquote(slug))


def file_identity(path: Path) -> dict | None:
    """Return stable device, inode, and content-prefix identity for a file."""
    try:
        stat = path.stat()
        with path.open("rb") as stream:
            head = stream.read(64)
    except OSError:
        return None
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "head_sha256": hashlib.sha256(head).hexdigest(),
    }


def same_file(left: dict | None, right: dict | None) -> bool:
    """Compare two file identities."""
    if not left or not right:
        return False
    if left.get("device") is not None and left.get("inode") is not None:
        return (
            left.get("device") == right.get("device")
            and left.get("inode") == right.get("inode")
            and left.get("head_sha256") == right.get("head_sha256")
        )
    return left.get("head_sha256") == right.get("head_sha256")


def rotated_file(path: Path, identity: dict) -> Path | None:
    """Find a rotated file matching a recorded identity."""
    candidates = sorted(path.parent.glob(path.name + ".*"))
    return next(
        (candidate for candidate in candidates if same_file(file_identity(candidate), identity)),
        None,
    )


def harvest(args) -> int:
    """Harvest route history into staging with watermark and duplicate tracking."""
    try:
        corpus = load(args.corpus, {"version": 1, "cases": []})
        staging = load(args.staging, [])
        watermark = load(args.watermark, {"offset": 0, "keys": [], "pending": []})
        hashes = [
            str(item) for item in (watermark.get("history", {}) or {}).get("prompt_hashes", [])
        ]
        offset = int(watermark.get("offset", 0))
        identity = (
            watermark.get("identity") if isinstance(watermark.get("identity"), dict) else None
        )
        keys = list(watermark.get("keys", []))
        keyset = {tuple(k) for k in keys if isinstance(k, list) and len(k) == 2}
        pending = [item for item in watermark.get("pending", []) if isinstance(item, dict)]
        dropped = int(watermark.get("pending_dropped", 0) or 0)
        covered = drift = new = 0
        corpus_cases = list(corpus.get("cases", []))
        history = watermark.get("history") if isinstance(watermark.get("history"), dict) else {}
        rejected = [str(item) for item in history.get("rejected_hashes", [])]
        previous_staging = (
            history.get("staging_hashes", {})
            if isinstance(history.get("staging_hashes", {}), dict)
            else {}
        )
        rejected = _reconcile_rejected(staging, corpus_cases, previous_staging, rejected)
        existing = corpus_cases + list(staging)
        additions = []

        def commit(key: tuple[str, int]) -> None:
            if key not in keyset:
                keyset.add(key)
                keys.append([key[0], key[1]])

        def process(record: dict) -> tuple[str, tuple[str, int] | None]:
            nonlocal covered, drift, new
            if record.get("event") != "user_prompt_submit":
                return "done", None
            try:
                sid, turn = str(record.get("session_id", "")), int(record.get("turn_id", 0) or 0)
            except (TypeError, ValueError):
                return "done", None
            key = (sid, turn)
            if not sid or not turn or key in keyset:
                return "done", key
            session = session_for(args.sessions_root, sid)
            if not session:
                return "unresolved", key
            summary_path = session / "summary.json"
            if not summary_path.exists():
                return "unresolved", key
            try:
                summary = load(summary_path, {})
            except (OSError, ValueError, json.JSONDecodeError):
                return "unresolved", key
            if summary.get("session_kind") in {"subagent", "subagent_resume"}:
                commit(key)
                return "done", key
            users = real_users(session)
            if turn > len(users):
                return "unresolved", key
            prompt = redact_text(re.sub(r"\s+", " ", users[turn - 1]).strip())[:400]
            obs_intent = record.get("intent")
            obs_role = record.get("role")
            candidates = [
                (similarity(prompt, str(item.get("prompt", ""))), item, True)
                for item in corpus_cases
            ] + [
                (similarity(prompt, str(item.get("prompt", ""))), item, False)
                for item in existing[len(corpus_cases) :]
            ]
            matched = max(candidates, default=(0.0, None, False), key=lambda item: item[0])
            flag = "negative-candidate" if obs_intent is None else None
            matched_expect = (
                matched[1].get("expect") if matched[1] is not None and matched[2] else None
            )
            if matched[0] >= 0.75 and flag is None and isinstance(matched_expect, dict):
                covered += 1
                commit(key)
                return "done", key
            if matched[0] >= 0.45 and flag is None:
                expected = matched_expect
                if isinstance(expected, dict) and (
                    obs_intent != expected.get("intent") or obs_role != expected.get("role")
                ):
                    flag = f"drift:{matched[1].get('id')}"
                    drift += 1
                else:
                    flag = f"near-dup:{matched[1].get('id')}"
            elif matched[0] < 0.45:
                new += 1
            item_id = _next_id(prompt, existing)
            observed = {
                "intent": obs_intent,
                "complexity": record.get("complexity"),
                "risk": record.get("risk"),
                "role": obs_role,
                "stages": stage_ids(record),
                "warnings_count": len(record.get("warnings") or []),
            }
            workspace = workspace_for(summary, session.parent.name)
            item = {
                "id": item_id,
                "harvested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id": sid,
                "turn_id": turn,
                "workspace": workspace,
                "prompt": prompt,
                "observed": observed,
                "git_changed": git_check(workspace, str(record.get("observed_at", "")))
                if args.git_check
                else None,
                "expect": None,
                "flag": flag,
                "notes": "",
            }
            additions.append(item)
            existing.append(item)
            commit(key)
            return "done", key

        retry = []
        for record in pending:
            status, _key = process(record)
            if status == "unresolved":
                retry.append(record)
        pending = retry

        current_identity = file_identity(args.route_log)
        sources: list[tuple[Path, int]] = []
        if current_identity is not None:
            if identity is None or same_file(identity, current_identity):
                size = args.route_log.stat().st_size
                if size < offset:
                    print(
                        f"warning: route log shrank before offset {offset}; unread interval was lost"
                    )
                start = offset if size >= offset else 0
                sources.append((args.route_log, start))
            else:
                old = rotated_file(args.route_log, identity)
                if old is not None:
                    size = old.stat().st_size
                    if size < offset:
                        print(
                            f"warning: rotated route log shrank before offset {offset}; unread interval was lost"
                        )
                    sources.append((old, offset if size >= offset else 0))
                sources.append((args.route_log, 0))
        elif identity is not None:
            old = rotated_file(args.route_log, identity)
            if old is not None:
                size = old.stat().st_size
                if size < offset:
                    print(
                        f"warning: rotated route log shrank before offset {offset}; unread interval was lost"
                    )
                sources.append((old, offset if size >= offset else 0))

        active_identity, active_offset = identity, offset
        blocked = False
        for source, start in sources:
            active_identity = file_identity(source)
            source_unresolved_offset = None
            active_offset = start
            with source.open("rb") as log:
                log.seek(start)
                while True:
                    record_start = log.tell()
                    raw = log.readline()
                    if not raw:
                        active_offset = (
                            source_unresolved_offset
                            if source_unresolved_offset is not None
                            else log.tell()
                        )
                        break
                    if not raw.endswith(b"\n"):
                        active_offset = (
                            source_unresolved_offset
                            if source_unresolved_offset is not None
                            else record_start
                        )
                        blocked = True
                        break
                    record_end = log.tell()
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        active_offset = record_end
                        continue
                    status, key = process(record)
                    if status == "unresolved":
                        if not any(
                            str(item.get("session_id", "")) == key[0]
                            and int(item.get("turn_id", 0) or 0) == key[1]
                            for item in pending
                        ):
                            pending.append(record)
                        if source_unresolved_offset is None:
                            source_unresolved_offset = record_start
                    else:
                        active_offset = record_end
            if blocked:
                break

        if len(pending) > PENDING_LIMIT:
            excess = len(pending) - PENDING_LIMIT
            pending = pending[:PENDING_LIMIT]
            dropped += excess
        staging, evicted, rejected = _append_staging(staging, additions, corpus_cases, rejected)
        state_hashes = list(dict.fromkeys(hashes))
        if len(state_hashes) > 8000:
            state_hashes = list(
                dict.fromkeys(
                    [
                        _staging_hash(item)
                        for item in corpus_cases + staging
                        if isinstance(item, dict)
                    ]
                    + rejected
                )
            )
        dump(args.staging, staging, private_dir=state_dir())
        keys = keys[-5000:]
        state = dict(watermark)
        state.update(
            {
                "offset": active_offset,
                "identity": active_identity,
                "keys": keys,
                "pending": pending,
                "pending_dropped": dropped,
                "history": {
                    "prompt_hashes": state_hashes[-8000:],
                    "rejected_hashes": rejected[-4000:],
                    "staging_hashes": {
                        str(x.get("id")): _staging_hash(x) for x in staging if isinstance(x, dict)
                    },
                },
                "last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        dump(args.watermark, state, private_dir=state_dir())
        print(
            f"new={new} covered={covered} drift={drift} pending={len(pending)} "
            f"pending_dropped={dropped} evicted={evicted} staging={args.staging}"
        )
        for item in additions:
            if str(item.get("flag", "")).startswith("drift:"):
                print(f"drift alert: {item['flag']}")
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


def _history_hash(text: str) -> str:
    normalized = " ".join(normalize(text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def classify_provenance(
    text: str,
    *,
    origin_kind=None,
    prompt_source=None,
    is_bash=False,
    session_kind=None,
) -> tuple[str, str]:
    """Classify a harvested record provenance from its available evidence."""
    if (
        origin_kind not in (None, "human")
        or prompt_source in {"system", "tool", "queued"}
        or is_bash
        or session_kind in {"subagent", "subagent_resume"}
    ):
        return "automation", "automation-metadata"
    syntax_markers = _AUTOMATION_SYNTAX
    if (
        any(marker in text for marker in syntax_markers)
        or any(text.startswith(prefix) for prefix in _AUTOMATION_PREFIXES)
        or text.startswith("/")
    ):
        return "automation", "automation-syntax"
    lowered = text.casefold()
    brief_markers = _AUTOMATION_BRIEF
    if text.startswith(("# ", "## ")) and sum(marker in lowered for marker in brief_markers) >= 2:
        return "automation", "automation-brief-structure"
    if origin_kind == "human" and prompt_source == "typed":
        return "human-typed", "human-provenance"
    if origin_kind is None and prompt_source is None and len(text) <= 400:
        cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if cyrillic > latin:
            return "human-typed", "human-weak-textual"
    return "uncertain", "uncertain-fallback"


def _parse_timestamp(value: str) -> datetime | None:
    try:
        stamp = parse_iso_utc(value)
    except (AttributeError, ValueError):
        return None
    return stamp


def git_evidence(workspace: str, timestamp: str) -> dict | None:
    """Inspect Git evidence associated with a harvested record."""
    if not workspace or not os.path.isdir(workspace):
        return None
    stamp = _parse_timestamp(timestamp)
    if stamp is None:
        return None
    until = stamp + timedelta(hours=48)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                workspace,
                "-c",
                "safe.directory=*",
                "log",
                f"--since={stamp.isoformat()}",
                f"--until={until.isoformat()}",
                "--pretty=%h|%s",
                "-n",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    sha, separator, subject = result.stdout.strip().partition("|")
    if not separator or not sha or not subject:
        return None
    return {"sha": sha, "subject": redact_text(subject)}


def _iso_after(value: str, since: datetime | None) -> bool:
    if since is None or not value:
        return True
    try:
        stamp = parse_iso_utc(value)
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return stamp >= since.astimezone(timezone.utc)


def _workspace_allowed(workspace: str, filters: set[str]) -> bool:
    return not filters or workspace in filters


def _claude_workspace(project: Path, record: dict) -> str:
    if record.get("cwd"):
        return str(record["cwd"])
    decoded = unquote(project.name)
    # Claude slugs cannot distinguish path hyphens from separators.
    return "/" + decoded[1:].replace("-", "/") if decoded.startswith("-") else decoded


def _grok_session_kind(root: Path, session_id: str) -> str | None:
    if not session_id:
        return None
    session = session_for(root, session_id)
    if session is None:
        return None
    try:
        summary = load(session / "summary.json", {})
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return summary.get("session_kind")


def _claude_prompt(record: dict) -> str:
    if record.get("type") != "user":
        return ""
    message = record.get("message")
    if (
        not isinstance(message, dict)
        or message.get("role") != "user"
        or not isinstance(message.get("content"), str)
    ):
        return ""
    return message["content"]


def _claude_provenance(record: dict) -> dict:
    origin = record.get("origin")
    return {
        "origin_kind": "automation"
        if record.get("isMeta") is True
        else (origin.get("kind") if isinstance(origin, dict) else origin),
        "prompt_source": record.get("promptSource"),
        "is_bash": bool(record.get("isBash") or record.get("isBashInput")),
        "session_kind": "subagent"
        if record.get("isSidechain") is True
        else record.get("sessionKind"),
    }


def _history_file_identity(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime_ns}


def _history_scan_needed(files: dict, key: str, identity: dict) -> bool:
    entry = files.get(key)
    return (
        not isinstance(entry, dict)
        or entry.get("size") != identity["size"]
        or entry.get("mtime") != identity["mtime"]
    )


def _priority(flag: str | None) -> int:
    if str(flag or "").startswith("drift:"):
        return 1
    return {
        "negative-candidate": 2,
        "git-derived": 3,
        "uncertain-origin+git": 4,
        "history-import": 5,
        "near-dup": 6,
    }.get(str(flag or ""), 7)


def _staging_hash(item: dict) -> str:
    return _history_hash(str(item.get("prompt", "")))


def _reconcile_rejected(
    staging: list, corpus_cases: list, previous: dict, rejected: list[str]
) -> list[str]:
    corpus_hashes = {_staging_hash(item) for item in corpus_cases if isinstance(item, dict)}
    current = {_staging_hash(item) for item in staging if isinstance(item, dict)}
    out = list(dict.fromkeys(str(item) for item in rejected))
    for item_id, digest in previous.items():
        if digest not in current and digest not in corpus_hashes:
            out.append(str(digest))
    return list(dict.fromkeys(out))[-4000:]


def _append_staging(
    staging: list, additions: list, corpus_cases: list, rejected: list[str]
) -> tuple[list, int, list[str]]:
    previous = {_staging_hash(item) for item in staging if isinstance(item, dict)}
    combined = staging + additions
    evicted = 0
    while len(combined) > 500:
        candidates = [(index, item) for index, item in enumerate(combined)]
        index, item = max(candidates, key=lambda pair: (_priority(pair[1].get("flag")), -pair[0]))
        combined.pop(index)
        digest = _staging_hash(item)
        if digest not in previous:
            rejected.append(digest)
        evicted += 1
    return combined, evicted, list(dict.fromkeys(rejected))[-4000:]


def _next_id(prompt: str, existing: list[dict]) -> str:
    prefix = category(prompt)
    maximum = max(
        [
            int(match.group(1))
            for item in existing
            if str(item.get("id", "")).startswith(prefix + "-")
            and (match := re.search(r"(\d+)$", str(item.get("id", ""))))
        ]
        or [0]
    )
    return f"{prefix}-{maximum + 1}"


def _observed(prompt: str, workspace: str, spec: dict, registry: dict) -> dict:
    decision = route_prompt(
        prompt,
        spec=spec,
        registry=registry,
        mode="static",
        workspace_root=workspace if os.path.isdir(workspace) else None,
    )
    return {
        "intent": decision.intent,
        "complexity": decision.complexity,
        "risk": decision.risk,
        "role": decision.role,
    }


def import_history(args) -> int:
    """Import eligible session history into the staging corpus."""
    try:
        corpus, staging, watermark, hashes, hashset, rejected = _load_sync_context(args)
        history = watermark.get("history") if isinstance(watermark.get("history"), dict) else {}
        files = history.get("files", {}) if isinstance(history.get("files", {}), dict) else {}
        git_keys = (
            history.get("git_commits", [])
            if isinstance(history.get("git_commits", []), list)
            else []
        )
        existing = list(corpus.get("cases", [])) + list(staging)
        additions: list[dict] = []
        counts = Counter()
        filters = {item.strip() for item in (args.workspaces or "").split(",") if item.strip()}
        since = parse_iso_utc(args.since) if args.since else None
        spec, registry = load_intents(), load_registry(config_path())

        def process(source: str, workspace: str, timestamp: str, prompt: str, **provenance) -> bool:
            nonlocal existing
            prompt = prompt.strip()
            if (
                len(prompt) < args.min_chars
                or not _iso_after(timestamp, since)
                or not _workspace_allowed(workspace, filters)
            ):
                return False
            cleaned = redact_text(prompt)
            compact = re.sub(r"\s+", " ", cleaned).strip()
            origin, rule = classify_provenance(prompt, **provenance)
            digest = _history_hash(compact)
            if digest in rejected or digest in hashset:
                return True
            if origin == "automation":
                counts[(source, rule)] += 1
                hashset.add(digest)
                hashes.append(digest)
                return True
            evidence = git_evidence(workspace, timestamp)
            if origin == "uncertain" and evidence is None and not args.keep_uncertain:
                counts[(source, "uncertain-skipped")] += 1
                return False
            counts[
                (
                    source,
                    "human"
                    if origin == "human-typed"
                    else "uncertain+git"
                    if evidence
                    else "uncertain-kept",
                )
            ] += 1
            observed = _observed(cleaned, workspace, spec, registry)
            corpus_count = len(corpus.get("cases", []))
            matches = [
                (similarity(compact, str(item.get("prompt", ""))), item, index >= corpus_count)
                for index, item in enumerate(existing)
            ]
            score, match, in_staging = max(
                matches, default=(0.0, None, False), key=lambda item: item[0]
            )
            flag = "negative-candidate" if observed["intent"] is None else None
            if origin == "uncertain":
                flag = "uncertain-origin+git" if evidence else "uncertain-origin"
            disposition = "new"
            if match is not None and (
                (score >= 0.45 and in_staging)
                or (score >= 0.75 and isinstance(match.get("expect"), dict) and flag is None)
            ):
                disposition = "covered"
            elif score >= 0.45 and match is not None:
                expected = match.get("expect")
                if (
                    isinstance(expected, dict)
                    and (
                        observed["intent"] != expected.get("intent")
                        or observed["role"] != expected.get("role")
                    )
                    and flag is None
                ):
                    disposition, flag = "drift", f"drift:{match.get('id')}"
                elif origin != "uncertain":
                    disposition, flag = "near-dup", flag or f"near-dup:{match.get('id')}"
            if disposition == "covered":
                counts[(source, "covered")] += 1
                hashset.add(digest)
                hashes.append(digest)
                return True
            item = _build_case(
                _next_id(compact, existing),
                source,
                workspace,
                timestamp,
                compact,
                origin,
                rule,
                observed,
                evidence,
                flag or "history-import",
            )
            additions.append(item)
            existing.append(item)
            counts[(source, disposition)] += 1
            hashset.add(digest)
            hashes.append(digest)
            return True

        processed = 0

        def scan(path: Path, source: str, workspace: str, record_prompt, provenance):
            nonlocal processed
            key, identity = str(path), _history_file_identity(path)
            if not _history_scan_needed(files, key, identity):
                return
            complete = True
            with path.open("rb") as stream:
                for raw in stream:
                    if not raw.endswith(b"\n"):
                        complete = False
                        continue
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    prompt = record_prompt(record)
                    if not prompt:
                        continue
                    processed += 1
                    if not process(
                        source,
                        workspace(record) if callable(workspace) else workspace,
                        str(
                            record.get("timestamp", record.get("message", {}).get("timestamp", ""))
                        ),
                        prompt,
                        **provenance(record),
                    ):
                        complete = False
            if complete:
                files[key] = identity

        for path in sorted(args.sessions_root.glob("*/prompt_history.jsonl")):
            scan(
                path,
                "grok-history",
                unquote(path.parent.name),
                lambda r: str(r.get("prompt", "")),
                lambda r: {
                    "is_bash": bool(r.get("is_bash") or r.get("isBash")),
                    "session_kind": _grok_session_kind(
                        args.sessions_root, str(r.get("session_id", ""))
                    ),
                },
            )
            if args.limit is not None and processed >= args.limit:
                break
        if not args.no_claude and (args.limit is None or processed < args.limit):
            for project in sorted(args.claude_root.iterdir()) if args.claude_root.exists() else []:
                if not project.is_dir():
                    continue
                for path in sorted(project.glob("*.jsonl")):
                    scan(
                        path,
                        "claude-history",
                        lambda r, project=project: _claude_workspace(project, r),
                        _claude_prompt,
                        _claude_provenance,
                    )
                if args.limit is not None and processed >= args.limit:
                    break
        staging, evicted, rejected = _append_staging(
            staging, additions, corpus.get("cases", []), rejected
        )
        if evicted:
            counts[("staging", "evicted")] += evicted
        state_hashes = list(dict.fromkeys(hashes))
        if len(state_hashes) > 8000:
            state_hashes = list(
                dict.fromkeys(
                    [
                        _staging_hash(item)
                        for item in corpus.get("cases", []) + staging
                        if isinstance(item, dict)
                    ]
                    + rejected
                )
            )
        state = dict(watermark)
        state["history"] = {
            "prompt_hashes": state_hashes[-8000:],
            "rejected_hashes": rejected[-4000:],
            "staging_hashes": {
                str(x.get("id")): _staging_hash(x) for x in staging if isinstance(x, dict)
            },
            "files": dict(list(files.items())[-500:]),
            "git_commits": git_keys[-8000:],
        }
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dump(args.staging, staging, private_dir=state_dir())
        dump(args.watermark, state, private_dir=state_dir())
        for (source, disposition), count in sorted(counts.items()):
            print(f"{source} {disposition}={count}")
        print(
            f"staged={len(additions)} evicted={evicted} staging={len(staging)} staging_path={args.staging}"
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


def _discover_workspaces(args) -> list[str]:
    requested = [item.strip() for item in (args.workspaces or "").split(",") if item.strip()]
    candidates = list(requested)
    if not requested:
        if args.sessions_root.exists():
            candidates.extend(
                unquote(path.name) for path in args.sessions_root.iterdir() if path.is_dir()
            )
        if args.claude_root.exists():
            for project in args.claude_root.iterdir():
                if not project.is_dir():
                    continue
                slug = unquote(project.name)
                candidates.append(
                    "/" + slug[1:].replace("-", "/") if slug.startswith("-") else slug
                )
                for session in project.glob("*.jsonl"):
                    try:
                        with session.open(encoding="utf-8") as stream:
                            for line in stream:
                                record = json.loads(line)
                                if record.get("cwd"):
                                    candidates.append(str(record["cwd"]))
                                    break
                    except (OSError, json.JSONDecodeError):
                        continue
    return list(dict.fromkeys(str(Path(item)) for item in candidates if os.path.isdir(item)))


def _git_subject_is_chore(subject: str) -> bool:
    return bool(
        re.match(r"^(?:Merge |Revert |bump(?:\b|[_:-]))", subject, re.IGNORECASE)
        or re.fullmatch(r"v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?", subject.strip())
    )


def import_git(args) -> int:
    """Import recent Git commits into the staging corpus."""
    try:
        corpus, staging, watermark, hashes, hashset, rejected = _load_sync_context(args)
        history = watermark.get("history") if isinstance(watermark.get("history"), dict) else {}
        files = history.get("files", {}) if isinstance(history.get("files", {}), dict) else {}
        git_keys = [
            item
            for item in history.get("git_commits", [])
            if isinstance(item, list) and len(item) == 2
        ]
        git_keyset = {(str(item[0]), str(item[1])) for item in git_keys}
        existing = list(corpus.get("cases", [])) + list(staging)
        additions: list[dict] = []
        spec = load_intents()
        registry = load_registry(config_path())
        since = args.since or "90 days ago"
        remaining = max(0, min(int(args.cap), 200))

        for workspace in _discover_workspaces(args):
            if remaining == 0:
                break
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        workspace,
                        "-c",
                        "safe.directory=*",
                        "log",
                        f"--since={since}",
                        "--pretty=%h|%ad|%s",
                        "--date=iso",
                        "-n",
                        str(remaining),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                print(f"workspace={workspace} commits=0 staged=0")
                continue
            if result.returncode:
                print(f"workspace={workspace} commits=0 staged=0")
                continue
            commits = 0
            staged = 0
            for line in result.stdout.splitlines():
                sha, separator, rest = line.partition("|")
                date, second_separator, subject = rest.partition("|")
                if not separator or not second_separator or not sha or not subject:
                    continue
                commits += 1
                key = (workspace, sha)
                if key in git_keyset or _git_subject_is_chore(subject):
                    continue
                compact = re.sub(r"\s+", " ", redact_text(subject)).strip()
                digest = _history_hash(compact)
                duplicate = (
                    digest in hashset
                    or digest in rejected
                    or any(
                        similarity(compact, str(item.get("prompt", ""))) >= 0.75
                        for item in existing
                    )
                )
                git_keyset.add(key)
                git_keys.append([workspace, sha])
                hashset.add(digest)
                hashes.append(digest)
                if duplicate:
                    continue
                evidence = {"sha": sha, "subject": compact}
                item = _build_case(
                    _next_id(compact, existing),
                    "git-history",
                    workspace,
                    date,
                    compact,
                    "git-commit",
                    "git-derived",
                    _observed(compact, workspace, spec, registry),
                    evidence,
                    "git-derived",
                )
                additions.append(item)
                existing.append(item)
                staged += 1
                remaining -= 1
                if remaining == 0:
                    break
            print(f"workspace={workspace} commits={commits} staged={staged}")

        staging, evicted, rejected = _append_staging(
            staging, additions, corpus.get("cases", []), rejected
        )
        state_hashes = list(dict.fromkeys(hashes))
        if len(state_hashes) > 8000:
            state_hashes = list(
                dict.fromkeys(
                    [
                        _staging_hash(item)
                        for item in corpus.get("cases", []) + staging
                        if isinstance(item, dict)
                    ]
                    + rejected
                )
            )
        state = dict(watermark)
        state["history"] = {
            "prompt_hashes": state_hashes[-8000:],
            "rejected_hashes": rejected[-4000:],
            "staging_hashes": {
                str(x.get("id")): _staging_hash(x) for x in staging if isinstance(x, dict)
            },
            "files": files,
            "git_commits": git_keys[-8000:],
        }
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        dump(args.staging, staging, private_dir=state_dir())
        dump(args.watermark, state, private_dir=state_dir())
        print(
            f"staged={len(additions)} evicted={evicted} staging={len(staging)} staging_path={args.staging}"
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


def merge(args) -> int:
    """Move adjudicated staging cases into the corpus."""
    corpus = load(args.corpus, {"version": 1, "cases": []})
    staging = load(args.staging, [])
    moved, remain = [], []
    for item in staging:
        expect = item.get("expect")
        if expect is None or (isinstance(expect, dict) and expect.get("intent") is None):
            remain.append(item)
            continue
        if (
            not isinstance(expect, dict)
            or expect.get("intent") not in INTENTS
            or (expect.get("role") is not None and expect.get("role") not in _known_roles())
        ):
            print(f"invalid expect for {item.get('id')}", file=sys.stderr)
            return 1
        if any(x.get("id") == item.get("id") for x in corpus.get("cases", [])):
            print(f"duplicate id: {item.get('id')}", file=sys.stderr)
            return 1
        case = {
            "id": item["id"],
            "prompt": item["prompt"],
            "expect": {"intent": expect.get("intent"), "role": expect.get("role")},
        }
        if item.get("flag"):
            case["flag"] = item["flag"]
        corpus["cases"].append(case)
        moved.append(item["id"])
    dump(args.corpus, corpus, private_dir=state_dir())
    dump(args.staging, remain, private_dir=state_dir())
    print(
        f"moved={len(moved)}"
        + (f" ids={','.join(moved)}" if moved else "")
        + "; run ./tests/grok-route-test.sh"
    )
    return 0


def status(args) -> int:
    """Print corpus staging and watermark status."""
    wm = load(args.watermark, {})
    staging = load(args.staging, [])
    corpus = load(args.corpus, {"cases": []})
    history = wm.get("history", {}) if isinstance(wm.get("history"), dict) else {}
    print(
        f"last_run={wm.get('last_run')} tracked_files={len(history.get('files', {}))} prompt_hashes={len(history.get('prompt_hashes', []))} rejected_hashes={len(history.get('rejected_hashes', []))} staging={len(staging)} corpus={len(corpus.get('cases', []))}"
    )
    print(
        "flags="
        + json.dumps(
            dict(Counter(x.get("flag") or "unflagged" for x in staging)),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def triage(args) -> int:
    """Print prioritized staging records for manual triage."""
    staging = load(args.staging, [])
    limit = max(0, args.limit if args.limit is not None else 50)
    ordered = sorted(
        enumerate(staging), key=lambda pair: (_priority(pair[1].get("flag")), pair[0])
    )[:limit]
    counts = Counter(item.get("flag") or "unflagged" for item in staging)
    for _, item in ordered:
        prompt = re.sub(r"\s+", " ", str(item.get("prompt", ""))).strip()[:90]
        print(
            f"id: {item.get('id')} flag: {item.get('flag')} priority: {_priority(item.get('flag'))} workspace: {item.get('workspace', '')}"
        )
        print(f"prompt: {prompt}")
        print("expect:")
    print("summary=" + json.dumps(dict(sorted(counts.items())), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    """Parse corpus-sync arguments and dispatch the selected operation."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("harvest", "import-history", "import-git", "merge", "status", "triage")
    )
    parser.add_argument("--route-log", type=Path, default=sidecar_path("route.jsonl"))
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument("--no-claude", action="store_true")
    parser.add_argument("--keep-uncertain", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cap", type=int, default=200)
    parser.add_argument("--min-chars", type=int, default=12)
    parser.add_argument("--since", default=None)
    parser.add_argument("--workspaces", default="")
    parser.add_argument("--corpus", type=Path, default=ROOT / "fixtures/workloads.json")
    parser.add_argument("--staging", type=Path, default=sidecar_path("workloads-staging.json"))
    parser.add_argument("--watermark", type=Path, default=sidecar_path("corpus-sync.json"))
    parser.add_argument("--git-check", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    _reset_prefixes_cache()
    _reset_known_roles_cache()
    handler = {
        "harvest": harvest,
        "import-history": import_history,
        "import-git": import_git,
        "merge": merge,
        "status": status,
        "triage": triage,
    }[args.command]
    if args.command == "triage":
        return handler(args)
    with state_lock(args.watermark):
        migrate_staging(args.staging)
        return handler(args)


if __name__ == "__main__":
    sys.exit(main())
