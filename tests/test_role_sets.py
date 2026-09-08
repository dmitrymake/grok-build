#!/usr/bin/env python3
"""Contract checks for the shared role-set definitions."""

from __future__ import annotations

from dataclasses import replace

from _harness import bootstrap

bootstrap()

import grokbuild.availability as availability
import grokbuild.gate as gate
import grokbuild.hook as hook
import grokbuild.pipeline as pipeline
import grokbuild.roles as roles
from grokbuild.state import RuntimeState


def test_tryout_catalog_models_are_not_role_routable() -> None:
    registry = roles.load_registry()
    model = next(iter(registry.provider_catalog))
    catalog = dict(registry.provider_catalog)
    catalog[model] = replace(catalog[model], tier="tryout")
    patched_roles = dict(registry._roles)
    meta = catalog[model]
    patched_roles["implement-hard"] = replace(
        patched_roles["implement-hard"],
        model=model,
        provider=meta.provider,
        provider_label=meta.provider_label,
        tier=meta.tier,
    )
    patched = registry.__class__(
        patched_roles, registry.known_models, registry.warnings, provider_catalog=catalog
    )
    available, signal = availability._role_availability(
        RuntimeState(), "implement-hard", patched, stale=False
    )
    assert not available
    assert signal is not None and signal.reason == "tryout models are not role-routable"
    issues = patched.validate_roles()
    assert any(
        issue["level"] == "error"
        and issue["role"] == "implement-hard"
        and "tryout" in issue["msg"]
        for issue in issues
    )


def test_role_sets() -> None:
    assert pipeline.READ_ONLY_STAGES == gate.READ_ONLY_ROLES == hook.READ_ONLY_ROLES
    assert pipeline.READ_ONLY_STAGES == roles.READ_ONLY_CANONICAL - roles.VISUAL_INTAKE_ROLES
    assert pipeline.IMPLEMENT_STAGES == roles.IMPLEMENT_ROLE_NAMES
    assert {
        "researcher",
        "researcher-analyst",
        "researcher-challenger",
    } <= roles.READ_ONLY_CANONICAL
    assert {
        "researcher",
        "researcher-analyst",
        "researcher-challenger",
    } <= roles.EXECUTION_READ_ONLY_ROLES
    assert "criterion-judge" in roles.READ_ONLY_CANONICAL
    assert "criterion-judge" in roles.EXECUTION_READ_ONLY_ROLES
    challengers = {"judge-challenger-agentic", "judge-challenger-structural"}
    assert challengers <= roles.READ_ONLY_CANONICAL
    assert challengers <= roles.EXECUTION_READ_ONLY_ROLES
    assert challengers.isdisjoint(roles.IMPLEMENT_ROLE_NAMES)


if __name__ == "__main__":
    test_role_sets()
    test_tryout_catalog_models_are_not_role_routable()
    print("ok   execution read-only role sets")
    print("ok   implementation role sets")
