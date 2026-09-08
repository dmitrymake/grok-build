"""Shared helpers for pytest and standalone test execution."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import tomllib
from io import BytesIO
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SID = "01a0063b-a2dd-7bf2-aafd-4a66cf8377cc"


def bootstrap() -> Path:
    """Put the repository root first on sys.path before package imports."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def make_check(failures: list[str] | None = None, *, style: str = "append") -> Callable:
    """Build a check helper while retaining each suite's failure contract."""

    def check(condition: bool, message: str) -> None:
        if condition:
            print(f"ok   {message}")
            return
        if style == "raise":
            print(f"FAIL {message}")
            raise AssertionError(message)
        if failures is not None:
            failures.append(message)
        print(f"FAIL {message}")

    return check


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text
        self.buffer = BytesIO(text.encode("utf-8"))

    def read(self) -> str:
        return self._text


def run_hook_main(
    payload: dict[str, Any], *, workspace_root: str | Path | None = None
) -> tuple[int, str]:
    """Run hook.main with JSON stdin and capture its stdout and exit status."""
    bootstrap()
    from grokbuild import hook as hook_route

    normalized = dict(payload)
    if workspace_root is not None:
        normalized.setdefault("workspaceRoot", str(workspace_root))
    old_stdin = sys.stdin
    output = io.StringIO()
    try:
        sys.stdin = _Stdin(json.dumps(normalized))
        with contextlib.redirect_stdout(output):
            try:
                result = hook_route.main()
            except SystemExit as exc:
                result = exc.code
    finally:
        sys.stdin = old_stdin
    return int(result or 0), output.getvalue()


def plant_session(
    grok_home: Path,
    prompt: str,
    model: str = "gpt-5.6-terra",
    *,
    session_id: str = SID,
    session_kind: str | None = None,
    metadata_in_info: bool = False,
) -> None:
    session = grok_home / "sessions" / "ws" / session_id
    session.mkdir(parents=True, exist_ok=True)
    if metadata_in_info:
        summary = {
            "info": {"id": session_id},
            "current_model_id": model,
            "session_kind": session_kind,
        }
    else:
        summary = {"info": {"id": session_id}, "current_model_id": model}
    (session / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (session / "chat_history.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "content": [{"type": "text", "text": f"<user_query>\n{prompt}\n</user_query>"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_hermetic_config(grok_home: Path) -> None:
    """Seed the isolated home with repository config; CI must not use account config."""
    grok_home.mkdir(parents=True, exist_ok=True)
    config_path = REPO_ROOT / "config" / "config.toml"
    shutil.copy2(config_path, grok_home / "config.toml")
    shutil.copytree(REPO_ROOT / "agents", grok_home / "agents", dirs_exist_ok=True)
    # CI is hermetic by design: presence-only dummy credentials enable routing
    # metadata checks without reading or emitting account credentials.
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for spec in config.get("model", {}).values():
        env_key = spec.get("env_key") if isinstance(spec, dict) else None
        if isinstance(env_key, str) and env_key:
            os.environ[env_key] = "hermetic-test-credential"


def setup_environment(tmp: Path) -> None:
    grok_home = tmp / "grok"
    os.environ["GROK_HOME"] = str(grok_home)
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")
    _seed_hermetic_config(grok_home)
    (tmp / "state" / "grok-route").mkdir(parents=True, exist_ok=True)
    for key in list(os.environ):
        if key.startswith("GROK_ROUTE_"):
            os.environ.pop(key, None)


def run_standalone(main: Callable[[], Any]) -> None:
    """Run a test main under the same hermetic environment as the pytest fixture."""
    bootstrap()
    environment = dict(os.environ)
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        os.environ["GROK_HOME"] = str(root / "grok-home")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        for key in list(os.environ):
            if key.startswith("GROK_ROUTE_"):
                os.environ.pop(key, None)
        _seed_hermetic_config(root / "grok-home")
        try:
            from grokbuild import conductor, gate, transcript

            (
                conductor.CONDUCTOR_DEFAULT,
                conductor.CONDUCTOR_FALLBACK,
                conductor.CONDUCTOR_EMERGENCY,
                conductor.CONDUCTOR_CANDIDATES,
            ) = conductor._load_conductor_config()
            gate._REGISTRY_CACHE = None
            transcript._TURN_COUNT_CACHE.clear()
            transcript._SESSION_KIND_CACHE = None
            result = main()
        except SystemExit as exc:
            result = exc.code
        finally:
            os.environ.clear()
            os.environ.update(environment)
            os.chdir(cwd)
    raise SystemExit(result or 0)
