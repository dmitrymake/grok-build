"""Pytest fixtures for the routing test suites."""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


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


# Test modules import routing globals during collection, before fixtures run.
_COLLECTION_GROK_HOME = Path(tempfile.mkdtemp(prefix="grok-test-home-"))
_seed_hermetic_config(_COLLECTION_GROK_HOME)
os.environ["GROK_HOME"] = str(_COLLECTION_GROK_HOME)


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _hermetic_routing_test(request, tmp_path):
    environment = dict(os.environ)
    cwd = os.getcwd()
    grok_home = tmp_path / "grok-home"
    _seed_hermetic_config(grok_home)
    os.environ["GROK_HOME"] = str(grok_home)
    os.environ["XDG_STATE_HOME"] = str(tmp_path / "state")
    os.environ["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    for key in list(os.environ):
        if key.startswith("GROK_ROUTE_"):
            os.environ.pop(key, None)
    from grokbuild import conductor

    (
        conductor.CONDUCTOR_DEFAULT,
        conductor.CONDUCTOR_FALLBACK,
        conductor.CONDUCTOR_EMERGENCY,
        conductor.CONDUCTOR_CANDIDATES,
    ) = conductor._load_conductor_config()
    failures = getattr(request.node.module, "FAILURES", None)
    if isinstance(failures, list):
        failures.clear()
    try:
        yield
    finally:
        try:
            try:
                from grokbuild import gate, transcript
            except ImportError:
                gate = transcript = None
            if gate is not None:
                gate._REGISTRY_CACHE = None
            if transcript is not None:
                transcript._TURN_COUNT_CACHE.clear()
                transcript._SESSION_KIND_CACHE = None
            if failures:
                pytest.fail("FAILURES: " + " | ".join(failures))
        finally:
            os.environ.clear()
            os.environ.update(environment)
            os.chdir(cwd)
