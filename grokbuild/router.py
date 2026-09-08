#!/usr/bin/env python3
"""Facade for the Grok Build combine router.

`route_prompt` is the single entry point shared by the hook and the CLI. It
builds a `Pipeline` with the caller's overrides and returns a `RouteDecision`.
"""

from __future__ import annotations

from typing import Any, Mapping

from grokbuild.decision import RouteDecision
from grokbuild.pipeline import Pipeline
from grokbuild.policy import Profile
from grokbuild.roles import RoleRegistry
from grokbuild.state import RuntimeState


def route_prompt(
    prompt: str,
    *,
    spec: Mapping[str, Any] | None = None,
    registry: RoleRegistry | None = None,
    profiles: Mapping[str, Profile] | None = None,
    profile_name: str | None = None,
    mode: str | None = None,
    state: RuntimeState | None = None,
    state_path: Any = None,
    log_path: Any = None,
    session_id: str | None = None,
    current_model: str | None = None,
    event: str = "resolve",
    source: str | None = None,
    persist: bool = False,
    enforce_default: bool = False,
    workspace_root: str | None = None,
    turn_id: int | None = None,
) -> RouteDecision:
    """Build the routing pipeline and return its decision."""
    pipeline = Pipeline(
        spec=spec,
        registry=registry,
        profiles=profiles,
        profile_name=profile_name,
        mode=mode,
        state=state,
        state_path=state_path,
        log_path=log_path,
        enforce_default=enforce_default,
    )
    if source is None:
        source = "static" if not persist else "hook"
    return pipeline.run(
        prompt,
        session_id=session_id,
        current_model=current_model,
        event=event,
        source=source,
        persist=persist,
        workspace_root=workspace_root,
        turn_id=turn_id,
    )


__all__ = ["route_prompt"]
