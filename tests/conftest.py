"""Pytest fixtures for the routing test suites."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _hermetic_routing_test(request, tmp_path):
    environment = dict(os.environ)
    cwd = os.getcwd()
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
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
