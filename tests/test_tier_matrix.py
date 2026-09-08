#!/usr/bin/env python3
"""Contract checks for provider-derived subscription tiers."""

from __future__ import annotations

import re
from pathlib import Path

from _harness import bootstrap

REPO_ROOT = Path(__file__).resolve().parents[1]

bootstrap()

from grokbuild.tiers import (
    TIER_PROVIDER_SUBSETS,
    achieved_level,
    load_provider_data,
    load_role_data,
    role_provider_map,
    roles_for_providers,
)


def test_primary_endpoint_projects_role_provider() -> None:
    providers = {
        "providers": {
            "container": {
                "models": {
                    "logical-model": {
                        "endpoints": [
                            {"provider": "primary-provider"},
                            {"provider": "reserve-provider"},
                        ]
                    }
                }
            }
        }
    }
    roles = {"implement-standard": {"model": "logical-model"}}
    assert role_provider_map(providers, roles) == {"implement-standard": "primary-provider"}


def test_readme_provider_role_matrix() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    rows = {}
    row_pattern = re.compile(
        r"^\| `([^`]+)`[^|]*\|[^|]*\|[^|]*\|([^|]*)\|$", re.MULTILINE
    )
    for provider, roles_text in row_pattern.findall(readme):
        rows[provider] = set(re.findall(r"`([^`]+)`", roles_text))
    providers = load_provider_data()
    role_map = role_provider_map(providers, load_role_data())
    for provider in providers["providers"]:
        assert rows[provider] == roles_for_providers({provider}, role_map)
    for provider in TIER_PROVIDER_SUBSETS["Full"]:
        assert provider in rows
    full_line = next(line for line in readme.splitlines() if line.startswith("- **Full"))
    assert "minimax" in full_line


def test_tier_matrix() -> None:
    providers = load_provider_data()
    role_map = role_provider_map(providers, load_role_data())
    expected_providers = {
        provider: roles_for_providers({provider}, role_map)
        for provider in providers["providers"]
    }
    for provider, expected in expected_providers.items():
        assert roles_for_providers({provider}, role_map) == expected
    assert TIER_PROVIDER_SUBSETS["Minimum"] == {"codex"}
    assert TIER_PROVIDER_SUBSETS["Recommended"] == {"codex", "zai", "opencode"}
    assert TIER_PROVIDER_SUBSETS["Full"] == set(providers["providers"])
    assert (
        roles_for_providers(TIER_PROVIDER_SUBSETS["Minimum"], role_map)
        == expected_providers["codex"]
    )
    assert roles_for_providers(TIER_PROVIDER_SUBSETS["Recommended"], role_map) == (
        expected_providers["codex"] | expected_providers["zai"] | expected_providers["opencode"]
    )
    assert roles_for_providers(TIER_PROVIDER_SUBSETS["Full"], role_map) == set(role_map)

    all_providers = set(providers["providers"])
    credentials = dict.fromkeys(all_providers, True)
    credentials["commandcode"] = False
    assert achieved_level(credentials, role_map, {"xai"})[0] == "Recommended"
    assert achieved_level(dict.fromkeys(all_providers, True), role_map, {"xai"})[0] == "Full"
    assert achieved_level({"codex": True}, role_map)[0] == "Minimum"
    assert (
        achieved_level(
            {provider: provider in {"codex", "zai", "opencode"} for provider in all_providers},
            role_map,
        )[0]
        == "Recommended"
    )


if __name__ == "__main__":
    test_primary_endpoint_projects_role_provider()
    test_readme_provider_role_matrix()
    test_tier_matrix()
    print("ok   provider-role matrix")
    print("ok   subscription tier subsets")
