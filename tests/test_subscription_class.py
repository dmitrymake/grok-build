#!/usr/bin/env python3
"""Provider subscription-class validation tests."""

from __future__ import annotations

from _harness import run_standalone

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

from grokbuild.render_docs import parse_providers  # noqa: E402
from grokbuild.roles import family_of, load_provider_catalog  # noqa: E402

PROVIDERS_PATH = REPO_ROOT / "grokbuild/providers.json"
VALID_CLASSES = {"primary", "reserve"}


def test_provider_classes_are_declared() -> None:
    data = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    providers = data["providers"]
    assert providers
    assert all(spec.get("subscription_class") in VALID_CLASSES for spec in providers.values())


def test_render_docs_accepts_provider_catalog() -> None:
    parsed = parse_providers(PROVIDERS_PATH)
    assert parsed
    assert {meta["subscription_class"] for meta in parsed.values()} <= VALID_CLASSES


def test_every_catalog_model_declares_a_family() -> None:
    raw = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    models = {
        model: spec
        for provider in raw["providers"].values()
        for model, spec in provider["models"].items()
    }
    assert models
    assert all(isinstance(spec.get("family"), str) and spec["family"] for spec in models.values())

    catalog = load_provider_catalog()
    assert set(catalog) == set(models)
    assert all(family_of(model, catalog) == spec["family"] for model, spec in models.items())


def test_model_families_preserve_declared_line_grouping() -> None:
    catalog = load_provider_catalog()
    assert family_of("gpt-5.6-sol", catalog) == family_of("gpt-5.6-luna", catalog)
    assert family_of("deepseek-v4-pro", catalog) == family_of("deepseek-v4-flash", catalog)
    assert family_of("glm-5.3", catalog) == family_of("glm-5.3-flash", catalog)
    assert len({family_of(model, catalog) for model in catalog}) < len(catalog)


def test_real_model_family_groups_are_complete() -> None:
    catalog = load_provider_catalog()
    expected = {
        "grok-4.6": "grok-4.6",
        "glm-5.3-flash": "glm-5.3",
        "deepseek-v4-pro": "deepseek-v4",
        "deepseek-v4-flash": "deepseek-v4",
        "qwen3.8-max": "qwen3.8",
        "kimi-k3": "kimi-k3",
        "minimax-m3": "minimax-m3",
    }
    assert {model: family_of(model, catalog) for model in expected} == expected


def test_multi_endpoint_models_project_the_primary_subscription() -> None:
    parsed = parse_providers(PROVIDERS_PATH)
    model = parsed["deepseek-v4-pro"]
    assert [endpoint["provider"] for endpoint in model["endpoints"]] == [
        "opencode",
        "commandcode",
    ]
    assert model["provider"] == "opencode"
    assert model["subscription_class"] == "primary"


def test_new_endpoint_models_declare_balance_and_primary_projection() -> None:
    parsed = parse_providers(PROVIDERS_PATH)
    expected = {
        "deepseek-v4-pro": ("opencode", "primary", "rotate"),
        "deepseek-v4-flash": ("opencode", "primary", "rotate"),
        "qwen3.8-max": ("opencode", "primary", "rotate"),
        "kimi-k3": ("opencode", "primary", "rotate"),
        "minimax-m3": ("minimax", "primary", "ordered"),
        "glm-5.3-flash": ("zai", "primary", "ordered"),
    }
    for model, (provider, subscription, balance) in expected.items():
        meta = parsed[model]
        assert meta["provider"] == provider, model
        assert meta["subscription_class"] == subscription, model
        assert meta["balance"] == balance, model
        assert [endpoint["provider"] for endpoint in meta["endpoints"]][0] == provider, model
        for endpoint in meta["endpoints"]:
            assert set(endpoint) == {
                "provider",
                "model_binding",
                "subscription_class",
                "auth",
                "credential_env",
                "require_availability_signal",
            }, model


def main() -> int:
    checks = [
        (test_provider_classes_are_declared, "every provider declares a valid subscription class"),
        (test_render_docs_accepts_provider_catalog, "render_docs accepts subscription classes"),
        (
            test_multi_endpoint_models_project_the_primary_subscription,
            "multi-endpoint models project their primary subscription",
        ),
        (
            test_new_endpoint_models_declare_balance_and_primary_projection,
            "new endpoint models declare balance and primary projection",
        ),
        (test_every_catalog_model_declares_a_family, "catalog models declare families"),
        (
            test_model_families_preserve_declared_line_grouping,
            "model families preserve declared line grouping",
        ),
        (test_real_model_family_groups_are_complete, "real model families are complete"),
        (
            test_rotated_endpoints_are_peer_pools_and_ordered_endpoints_descend,
            "rotated endpoints are peers",
        ),
    ]
    failures = 0
    for check, label in checks:
        try:
            check()
        except AssertionError:
            print(f"FAIL {label}")
            failures += 1
        else:
            print(f"ok   {label}")
    return failures


def test_rotated_endpoints_are_peer_pools_and_ordered_endpoints_descend() -> None:
    """Endpoint balance must agree with the pools' provider-level classes.

    ``rotate`` starts the endpoint walk at a per-session index, so half of all
    sessions hit the second endpoint first: that is only honest when both
    endpoints sit on pools of the same subscription class (peer pools), and
    then the endpoint-level ``primary``/``reserve`` labels record declaration
    order, not priority. ``ordered`` keeps the declared primary first, so a
    later endpoint may not belong to a higher class than an earlier one.
    """
    data = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    provider_class = {name: spec["subscription_class"] for name, spec in data["providers"].items()}
    rank = {"primary": 0, "reserve": 1}
    for model, meta in parse_providers(PROVIDERS_PATH).items():
        endpoints = meta.get("endpoints") or []
        if len(endpoints) < 2:
            continue
        classes = [provider_class[endpoint["provider"]] for endpoint in endpoints]
        if meta["balance"] == "rotate":
            assert len(set(classes)) == 1, f"{model}: rotate across non-peer pools {classes}"
        else:
            ranks = [rank[item] for item in classes]
            assert ranks == sorted(ranks), f"{model}: ordered endpoints escalate class {classes}"


if __name__ == "__main__":
    run_standalone(main)
