#!/usr/bin/env python3
"""Offline R1 composition and fixture-contract tests."""

from __future__ import annotations

from _harness import run_standalone

import json
import re

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
from eval.composition import (
    MODEL_REGISTRY,
    VARIANTS,
    account_trace,
    compose_r9_snapshot,
    compose_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


def test_variant_namespaces_are_complete():
    assert set(VARIANTS) == {
        "single-best-agent",
        "full-pipeline",
        "no-recon",
        "no-LLM-review",
        "verifier-only",
        "same-provider-reviewer",
        "cross-provider-reviewer",
        "fixed-topology",
        "adaptive-topology",
        "recon-configured",
        "recon-width-minus-1",
        "recon-same-lens",
        "recon-diverse-lens",
        "research-width-minus-1",
        "recon-risk-swap",
        "recon-risk-additive",
    }


def test_single_best_is_one_agent():
    assert [s["role"] for s in compose_snapshot("single-best-agent")["stages"]] == [
        "implement-standard",
        "verify",
    ]


def test_full_pipeline_has_recon_review_verifier():
    assert [s["stage_id"] or s["role"] for s in compose_snapshot("full-pipeline")["stages"]] == [
        "recon",
        "implement-standard",
        "review-hard",
        "verify/0",
    ]


def test_no_recon_removes_recon_only():
    assert all(s["stage_id"] != "recon" for s in compose_snapshot("no-recon")["stages"])


def test_no_llm_review_retains_recon_and_verifier():
    roles = [s["role"] for s in compose_snapshot("no-LLM-review")["stages"]]
    assert roles == ["recon", "implement-standard", "verify"]


def test_verifier_only_keeps_agent_and_verifier():
    assert len(compose_snapshot("verifier-only")["stages"]) == 2


def test_provider_transforms_are_resolved_in_snapshots():
    same = compose_snapshot("same-provider-reviewer")
    cross = compose_snapshot("cross-provider-reviewer")
    assert (
        same["roles"]["review-hard"]["provider"] == same["roles"]["implement-standard"]["provider"]
    )
    assert (
        cross["roles"]["review-hard"]["provider"]
        != cross["roles"]["implement-standard"]["provider"]
    )
    assert cross["roles"]["review-hard"]["available"] is True
    assert cross["roles"]["review-hard"]["model"] != cross["roles"]["implement-standard"]["model"]
    assert "synthetic-review-alt" in MODEL_REGISTRY
    assert cross["roles"]["review-hard"] == MODEL_REGISTRY["synthetic-review-alt"]


def test_all_fixtures_all_executable_variants_compose():
    fixtures = [
        "fictional-calculator-001.json",
        "fictional-dashboard-002.json",
        "fictional-sensor-003.json",
    ]
    executable = [
        name
        for name in VARIANTS
        if name
        not in {
            "adaptive-topology",
            "recon-configured",
            "recon-width-minus-1",
            "recon-same-lens",
            "recon-diverse-lens",
            "research-width-minus-1",
        }
    ]
    for fixture in fixtures:
        for variant in executable:
            snapshot = compose_snapshot(variant, fixture)
            assert snapshot["executable"] is True
            assert all(
                role in snapshot["roles"]
                for stage in snapshot["stages"]
                if stage["kind"] != "verify"
                for role in (
                    [stage["role"]]
                    if not stage["members"]
                    else [member["role"] for member in stage["members"]]
                )
            )


def test_fixture_shapes_vary_by_complexity():
    low = compose_snapshot("full-pipeline", "fictional-calculator-001.json")
    medium = compose_snapshot("full-pipeline", "fictional-dashboard-002.json")
    high = compose_snapshot("full-pipeline", "fictional-sensor-003.json")
    assert low["stages"][0]["role"] == "explore" and low["stages"][0]["kind"] == "spawn"
    assert medium["stages"][0]["stage_id"] == "recon"
    assert high["stages"][0]["stage_id"] == "recon" and len(high["stages"][0]["members"]) == 5


def test_zero_provider_calls_and_tokens():
    for fixture in (
        "fictional-calculator-001.json",
        "fictional-dashboard-002.json",
        "fictional-sensor-003.json",
    ):
        for variant in VARIANTS:
            snapshot = (
                compose_snapshot(variant, fixture)
                if variant
                not in {
                    "recon-configured",
                    "recon-width-minus-1",
                    "recon-same-lens",
                    "recon-diverse-lens",
                    "research-width-minus-1",
                }
                else compose_r9_snapshot(variant, fixture)
            )
            assert snapshot["provider_counters"] == {
                "provider_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }


def test_fixed_topology_is_executable():
    assert compose_snapshot("fixed-topology")["executable"] is True


def test_adaptive_topology_is_reserved_not_executable():
    assert compose_snapshot("adaptive-topology")["executable"] is False


def test_recon_configured_width_and_ids():
    stage = next(
        x for x in compose_r9_snapshot("recon-configured")["stages"] if x["stage_id"] == "recon"
    )
    assert [x["member_id"] for x in stage["members"]] == ["recon/0", "recon/1", "recon/2"]


def test_recon_width_minus_one():
    stage = next(
        x for x in compose_r9_snapshot("recon-width-minus-1")["stages"] if x["stage_id"] == "recon"
    )
    assert len(stage["members"]) == 2


def test_recon_same_lens_repeats_first():
    members = next(
        x for x in compose_r9_snapshot("recon-same-lens")["stages"] if x["stage_id"] == "recon"
    )["members"]
    assert len({(x["role"], x["reason"]) for x in members}) == 1


def test_recon_diverse_lens_uses_pool_order():
    members = next(
        x for x in compose_r9_snapshot("recon-diverse-lens")["stages"] if x["stage_id"] == "recon"
    )["members"]
    assert len({x["role"] for x in members}) >= 2


def test_risk_swap_replaces_last_member_at_fixed_width():
    snapshot = compose_r9_snapshot("recon-risk-swap")
    assert [stage["stage_id"] or stage["role"] for stage in snapshot["stages"]] == [
        "recon",
        "implement-standard",
        "review-hard",
        "verify/0",
    ]
    baseline = next(
        x for x in compose_r9_snapshot("recon-configured")["stages"] if x["stage_id"] == "recon"
    )["members"]
    members = next(x for x in snapshot["stages"] if x["stage_id"] == "recon")["members"]
    assert [x["role"] for x in members] == ["explore", "explore", "explore-risk"]
    assert [x["member_id"] for x in members] == ["recon/0", "recon/1", "recon/2"]
    assert len(members) == len(baseline) == 3
    assert sum(x["role"] == "explore-risk" for x in members) == 1
    assert members[:2] == baseline[:2]


def test_risk_additive_appends_one_member():
    snapshot = compose_r9_snapshot("recon-risk-additive")
    assert [stage["stage_id"] or stage["role"] for stage in snapshot["stages"]] == [
        "recon",
        "implement-standard",
        "review-hard",
        "verify/0",
    ]
    baseline = next(
        x for x in compose_r9_snapshot("recon-configured")["stages"] if x["stage_id"] == "recon"
    )["members"]
    members = next(x for x in snapshot["stages"] if x["stage_id"] == "recon")["members"]
    assert [x["role"] for x in members] == [
        "explore",
        "explore",
        "explore-thorough",
        "explore-risk",
    ]
    assert [x["member_id"] for x in members] == [
        "recon/0",
        "recon/1",
        "recon/2",
        "recon/3",
    ]
    assert members[:3] == baseline
    assert sum(x["role"] == "explore-risk" for x in members) == 1


def test_explore_risk_registry_binding_is_exact():
    assert MODEL_REGISTRY["explore-risk"] == {
        "model": "minimax-m3",
        "provider": "provider-c",
        "effort": "high",
        "available": True,
    }


def test_research_width_minus_one():
    stage = next(
        x
        for x in compose_r9_snapshot("research-width-minus-1")["stages"]
        if x["stage_id"] == "research"
    )
    assert len(stage["members"]) == 2 and all(x["required"] for x in stage["members"])


def test_production_modules_do_not_import_eval():
    import_pattern = re.compile(
        r"^\s*(?:from\s+eval(?:\.|\s)|import\s+eval(?:\.|\s|$))", re.MULTILINE
    )
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "grokbuild").glob("*.py"))
        if import_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_synthetic_trace_accounting():
    assert account_trace(
        [
            {"stage": "recon", "member": "recon/0", "state": "observed"},
            {"stage": "recon", "member": "recon/1", "state": "missing"},
        ]
    ) == {"planned": 2, "observed": 1, "missing": 1}


def test_malformed_synthetic_trace_fails():
    try:
        account_trace([{"stage": "recon"}])
    except ValueError as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("malformed trace accepted")


def _blacklist_terms() -> list[str]:
    """Terms from the tracked (format-only) lists plus their private local siblings.

    The real lists are untracked ``*.local.txt`` files that only the release
    machine carries; a clone without them has no terms to check against.
    """
    names = ("blacklist-l1", "blacklist-l2")
    paths = [
        ROOT / "scripts" / "release-check.d" / f"{name}{suffix}"
        for name in names
        for suffix in (".txt", ".local.txt")
    ]
    return [
        line.strip()
        for path in paths
        if path.is_file()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_tracked_blacklists_carry_no_terms():
    """The tracked lists document the format; publishing the terms defeats them."""
    for name in ("blacklist-l1.txt", "blacklist-l2.txt"):
        path = ROOT / "scripts" / "release-check.d" / name
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines and all(line.lstrip().startswith("#") for line in lines), name


def test_blacklist_term_lists_are_non_empty():
    terms = _blacklist_terms()
    if not terms:
        pytest.skip("private release-check term lists are not installed on this machine")
    assert terms


def test_fixture_schema_and_depersonalization():
    terms = _blacklist_terms()
    # ai-console is an additional local non-blacklisted probe term, not a Gate term.
    terms.append("ai-console")
    forbidden = re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, terms)) + r")(?![A-Za-z0-9_])"
    )
    paths = sorted((ROOT / "eval/tasks").glob("*.json"))
    assert len(paths) == 3
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert {
            "schema_version",
            "id",
            "version",
            "prompt",
            "task_class",
            "complexity",
            "path_scope",
            "rubric",
            "hidden_verifier",
            "acceptance",
        } <= raw.keys()
        assert set(raw["rubric"]) == {"ref", "version"} and set(raw["hidden_verifier"]) == {
            "ref",
            "version",
        }
        assert not forbidden.search(path.read_text(encoding="utf-8"))
        assert all(item["kind"] in {"pytest", "file_contains"} for item in raw["acceptance"])


def main():
    import pytest

    result = pytest.main([__file__, "-q"])
    if result == 0:
        for name in [name for name in globals() if name.startswith("test_")]:
            print(f"ok {name}")
    raise SystemExit(result)


if __name__ == "__main__":
    run_standalone(main)


PYTEST_ONLY = (
    "test_variant_namespaces_are_complete",
    "test_single_best_is_one_agent",
    "test_full_pipeline_has_recon_review_verifier",
    "test_no_recon_removes_recon_only",
    "test_no_llm_review_retains_recon_and_verifier",
    "test_verifier_only_keeps_agent_and_verifier",
    "test_provider_transforms_are_resolved_in_snapshots",
    "test_all_fixtures_all_executable_variants_compose",
    "test_fixture_shapes_vary_by_complexity",
    "test_zero_provider_calls_and_tokens",
    "test_fixed_topology_is_executable",
    "test_adaptive_topology_is_reserved_not_executable",
    "test_recon_configured_width_and_ids",
    "test_recon_width_minus_one",
    "test_recon_same_lens_repeats_first",
    "test_recon_diverse_lens_uses_pool_order",
    "test_risk_swap_replaces_last_member_at_fixed_width",
    "test_risk_additive_appends_one_member",
    "test_explore_risk_registry_binding_is_exact",
    "test_research_width_minus_one",
    "test_production_modules_do_not_import_eval",
    "test_synthetic_trace_accounting",
    "test_malformed_synthetic_trace_fails",
    "test_tracked_blacklists_carry_no_terms",
    "test_blacklist_term_lists_are_non_empty",
    "test_fixture_schema_and_depersonalization",
)
