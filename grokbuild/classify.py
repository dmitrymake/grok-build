#!/usr/bin/env python3
"""Dictionary router for Grok Build intents. No third-party deps."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
INTENTS_PATH = ROOT / "intents.json"

USER_QUERY_FULL_RE = re.compile(r"\s*<user_query>\s*(.*?)\s*</user_query>\s*", re.DOTALL)
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
USER_INFO_RE = re.compile(r"<user_info>.*?</user_info>", re.DOTALL)
STOP_FEEDBACK_MARK = "[[GROK_ROUTE_STOP_FEEDBACK:v1]]"


def is_synthetic_stop_feedback(text: str) -> bool:
    """Recognize only a complete hook-generated Stop-feedback turn."""
    normalized = " ".join((text or "").split())
    return bool(
        re.fullmatch(
            rf"route=[a-z-]+: NEXT: .* {re.escape(STOP_FEEDBACK_MARK)}\.",
            normalized,
        )
    )


def load_intents(path: Path | None = None) -> dict[str, Any]:
    target = path or INTENTS_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    return (text or "").casefold().replace("ё", "е")


def extract_user_text(text: str) -> str:
    """Return only the user's own words.

    ``<system-reminder>`` and ``<user_info>`` blocks are harness-injected
    context and are stripped so they can never score. A ``<user_query>`` body
    is authoritative only when it is the *outermost* wrapper of the message; an
    untrusted message that embeds a fake ``<user_query>`` (or fake reminder/
    info blocks) inside its own text falls back to the wrapper-stripped raw
    text, so the real prompt still classifies.
    """
    blob = text or ""
    body = SYSTEM_REMINDER_RE.sub("", blob)
    body = USER_INFO_RE.sub("", body)
    match = USER_QUERY_FULL_RE.fullmatch(body)
    if match:
        return match.group(1).strip()
    return body.strip()


def extract_user_text_raw(text: str) -> str:
    """Return the user's own words *without* stripping harness-tag content.

    ``<system-reminder>`` and ``<user_info>`` blocks are preserved as scoreable
    text; only the outermost ``<user_query>`` wrapper is authoritative. This is
    the conservative raw view scored alongside the stripped ``extract_user_text``
    so a fake mid-prompt reminder/info block cannot suppress a strong intent
    signal.
    """
    blob = text or ""
    match = USER_QUERY_FULL_RE.fullmatch(blob)
    if match:
        return match.group(1).strip()
    return blob.strip()


def _hits(text: str, needles: list[str]) -> list[str]:
    found: list[str] = []
    for raw in needles:
        needle = normalize(raw)
        if not needle:
            continue
        if " " in needle or "-" in needle:
            if needle in text:
                found.append(raw)
            continue
        # Prefix-of-token matching lets Russian stems match inflected forms.
        if re.search(
            rf"(?<![0-9a-zа-я_]){re.escape(needle)}"
            + (r"(?![0-9a-zа-я_])" if needle.isascii() else ""),
            text,
        ):
            found.append(raw)
    return found


def match_needles(text: str, needles: list[str]) -> list[str]:
    """Public alias for _hits; the feature extractor and policy import it."""
    return _hits(text, needles)


def model_allowed(current: str | None, required: str | None) -> bool:
    if not required:
        return True
    if not current:
        return False
    left = current.strip()
    right = required.strip()
    if left == right:
        return True
    return False


if __name__ == "__main__":
    import sys

    # Keep the module entry point on the canonical CLI; cli.main accepts argv explicitly.
    from grokbuild.cli import main as cli_main

    raise SystemExit(cli_main(["explain", "--json", *sys.argv[1:]]))
