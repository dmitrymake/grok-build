#!/usr/bin/env python3
"""Contract corpus for representative recurring workloads."""

from __future__ import annotations

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"
REPO_CONFIG = REPO_ROOT / "config" / "config.toml"

import json


from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.roles import load_registry  # noqa: E402
from grokbuild.router import route_prompt  # noqa: E402

FIXTURE = ROOT / "fixtures" / "workloads.json"
REPRESENTATIVE = {
    "data-05": ["recon", "plan-hard", "implement-hard", "review-hard", "verify", "verify"],
    "data-06": ["explore", "implement-standard", "verify", "verify"],
    "fw-16": ["explore", "implement-ops", "review-hard", "verify", "verify"],
    "df-27": ["explore", "implement-standard", "verify", "verify"],
    "df-32": ["explore", "implement-standard", "verify", "verify"],
    "ms-47": ["recon", "plan-hard", "implement-hard", "review-hard", "verify", "verify"],
}
REPRESENTATIVE_CLASSIFICATION = {
    "data-05": ("high", "high"),
    "data-06": ("low", "medium"),
    "fw-16": ("low", "high"),
    "df-27": ("low", "medium"),
    "df-32": ("low", "medium"),
    "ms-47": ("high", "high"),
}
WORKLOAD_FAILED = 0
WORKLOAD_TOTAL = 0


def test_workloads() -> None:
    global WORKLOAD_FAILED, WORKLOAD_TOTAL
    corpus = json.loads(FIXTURE.read_text(encoding="utf-8"))
    failed = 0
    total = len(corpus["cases"])
    WORKLOAD_TOTAL = total
    spec = load_intents()
    registry = load_registry(REPO_CONFIG)
    for case in corpus["cases"]:
        expected = case["expect"]
        decision = route_prompt(
            case["prompt"],
            spec=spec,
            mode="static",
            workspace_root=str(REPO_ROOT),
            registry=registry,
        )
        errors = []
        if decision.intent != expected["intent"]:
            errors.append(f"intent expected={expected['intent']!s} got={decision.intent!s}")
        if expected.get("role") is not None and decision.role != expected["role"]:
            errors.append(f"role expected={expected['role']} got={decision.role}")
        if decision.intent == "implement":
            roles = [stage.role for stage in decision.execution]
            if decision.complexity in {"medium", "high"}:
                review_present = any(
                    role in {"review", "review-hard", "review-independent"} for role in roles
                )
                if not review_present and not (
                    not decision.role_spawnable
                    and any("review" in warning.lower() for warning in decision.warnings)
                ):
                    errors.append(f"medium/high implementation has no review stage: {roles}")
            if (
                any(word in case["prompt"].lower() for word in ("прошей", "sysupgrade"))
                and decision.role != "implement-ops"
            ):
                errors.append(
                    f"destructive workload must select implement-ops, got {decision.role}"
                )
        if case["id"] in REPRESENTATIVE:
            expected_complexity, expected_risk = REPRESENTATIVE_CLASSIFICATION[case["id"]]
            if decision.complexity != expected_complexity:
                errors.append(
                    f"complexity expected={expected_complexity} got={decision.complexity}"
                )
            if decision.risk != expected_risk:
                errors.append(f"risk expected={expected_risk} got={decision.risk}")
            actual = [stage.role for stage in decision.execution]
            if actual != REPRESENTATIVE[case["id"]]:
                errors.append(f"execution expected={REPRESENTATIVE[case['id']]} got={actual}")
        if errors:
            failed += 1
        detail = "; ".join(errors) if errors else f"{decision.intent}/{decision.role}"
        print(f"{'ok' if not errors else 'FAIL':4} {case['id']:7} {detail}")
    WORKLOAD_FAILED = failed
    if failed:
        raise AssertionError(f"{failed} workload failures")


def main() -> int:
    global WORKLOAD_FAILED, WORKLOAD_TOTAL
    try:
        test_workloads()
    except AssertionError:
        pass
    print(f"{WORKLOAD_TOTAL - WORKLOAD_FAILED}/{WORKLOAD_TOTAL} passed")
    return 1 if WORKLOAD_FAILED else 0


if __name__ == "__main__":
    run_standalone(main)
