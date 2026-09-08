#!/usr/bin/env python3
"""Verifier command parsing and workspace configuration."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from typing import Any, Mapping

from grokbuild._repo import repo_root
from grokbuild.roles import resolve_config_path

VERIFIER_SENTINEL = "$REPO_ROOT"


def _argv_has_shell_metachar(argv: tuple[str, ...]) -> bool:
    """Return whether tokens carry rejected shell metacharacters or controls."""
    return any(
        any(ch in token for ch in ("\n", "\r", "\x00", "|", "&", ";", "<", ">", "`", "$("))
        for token in argv
    )


def parse_verifier_argv(value: str) -> tuple[str, ...] | None:
    """Shlex-split a configured verifier command into its exact argv.

    Returns None for unparsable input or any wrapper/metacharacter, so a
    configured verifier can never smuggle in an interpreter or extra args.
    """
    try:
        argv = tuple(shlex.split(value or ""))
    except ValueError:
        return None
    if not argv or _argv_has_shell_metachar(argv):
        return None
    return argv


def _verifier_executable(root: Path, argv: tuple[str, ...]) -> bool:
    target = Path(argv[0])
    if not target.is_absolute():
        target = root / target
    try:
        target = target.resolve()
    except OSError:
        return False
    return target.is_file() and os.access(target, os.X_OK)


def _sentinel_root() -> Path:
    return repo_root()


def load_verifier_map() -> dict[str, Any]:
    """Load workspace verifiers from the effective config without failing routing."""
    try:
        config_path = resolve_config_path()
        if config_path is None:
            return {}
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        routing = config.get("routing", {})
        if not isinstance(routing, Mapping):
            return {}
        verifiers = routing.get("verifiers", {})
        if not isinstance(verifiers, Mapping):
            return {}
        return dict(verifiers)
    except Exception:
        return {}


def apply_config_verifiers(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    """Overlay configured verifier entries on the supplied spec mapping."""
    config_map = load_verifier_map()
    if not config_map:
        return spec
    legacy = spec.get("verifiers") or {}
    if not isinstance(legacy, Mapping):
        legacy = {}
    merged = dict(spec)
    merged["verifiers"] = {**legacy, **config_map}
    return merged


def resolve_verifier(
    workspace_root: str | None,
    spec: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Return canonical verifier command lines for a workspace, or None.

    A verify stage is only composed when the workspace has an explicitly
    configured verifier whose argv[0] is an executable file. There is no global
    default verifier: arbitrary repos never inherit the dotfiles test runner.
    """
    if not workspace_root:
        return None
    root = Path(str(workspace_root))
    try:
        root = root.resolve()
    except OSError:
        return None
    verifiers = spec.get("verifiers", {}) if isinstance(spec, Mapping) else {}
    if not isinstance(verifiers, Mapping):
        return None
    for key, value in verifiers.items():
        if isinstance(value, str):
            values = (value,)
        elif isinstance(value, list):
            values = tuple(value)
        else:
            continue
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            continue
        key_text = str(key)
        if key_text == VERIFIER_SENTINEL:
            matched = root == _sentinel_root()
        else:
            key_path = Path(key_text)
            if not key_path.is_absolute():
                continue
            try:
                matched = str(key_path.resolve()) == str(root)
            except OSError:
                matched = False
        if not matched:
            continue
        commands: list[str] = []
        for item in values:
            argv = parse_verifier_argv(item)
            if argv is None or not _verifier_executable(root, argv):
                break
            commands.append(shlex.join(argv))
        else:
            return tuple(commands)
    return None


def is_verifier_command(command: str | None, expected: str) -> bool:
    """True only when ``command`` shlex-parses to exactly the configured argv."""
    if not expected:
        return False
    expected_argv = parse_verifier_argv(expected)
    if expected_argv is None:
        return False
    try:
        actual_argv = tuple(shlex.split(command or ""))
    except ValueError:
        return False
    if _argv_has_shell_metachar(actual_argv):
        return False
    return actual_argv == expected_argv
