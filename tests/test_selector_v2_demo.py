#!/usr/bin/env python3
"""Reproducible offline demonstration of selector v2 and supply escalation."""

from __future__ import annotations

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

from grokbuild.artifact_judge import Candidate, Requirement, TaskContract, Verdict  # noqa: E402
from grokbuild.datasets import EscalationRecord  # noqa: E402
from grokbuild.frontier import FrontierSignals, decide  # noqa: E402
from grokbuild.selector import (  # noqa: E402
    CONSENSUS_OVERTURN_CONFIDENCE,
    cluster_artifacts,
    select_v2,
)

FAILURES: list[str] = []
CONTRACT = TaskContract("offline-demo", requirements=(Requirement("verified"),))


check = make_check(FAILURES)


def candidate(label: str, content: str) -> Candidate:
    return Candidate(label, {"change.diff": content}, hard_results={"verified": True})


def test_selector_v2_offline_demo() -> None:
    majority = [candidate(label, "+return value\n") for label in ("A", "B", "C")]
    minority = candidate("D", "+return value.strip()\n")
    clusters = cluster_artifacts((*majority, minority), CONTRACT)
    check(
        {(cluster.members, cluster.support) for cluster in clusters}
        == {(("A", "B", "C"), 3), (("D",), 1)},
        "clustering returns the deterministic partition and support 3+1",
    )

    judge_calls = 0

    def forbidden(_first: Candidate, _second: Candidate) -> Verdict:
        nonlocal judge_calls
        judge_calls += 1
        raise AssertionError("dominant consensus must not invoke a judge")

    dominant = select_v2(CONTRACT, (*majority, minority), forbidden)
    check(
        dominant.selected == "A" and dominant.comparisons == 0 and judge_calls == 0,
        "complete 3+1 portfolio selects A with comparisons=0",
    )

    tied = (
        candidate("common", "+return value\n"),
        candidate("minority", "+return value.strip()\n"),
    )
    panel_calls: list[tuple[str, str]] = []

    def panel(first: Candidate, second: Candidate) -> Verdict:
        panel_calls.append((first.label, second.label))
        return Verdict(
            "a" if first.label == "minority" else "b",
            confidence=CONSENSUS_OVERTURN_CONFIDENCE,
            reason_codes=("judges_agree",),
        )

    corrected = select_v2(CONTRACT, tied, panel)
    check(
        corrected.selected == "minority"
        and corrected.comparisons == 2
        and panel_calls[1] == tuple(reversed(panel_calls[0])),
        "two reversed-order panel verdicts select the tied-support minority-correct artifact",
    )

    supply = 0.0
    record = EscalationRecord(
        decision_id="offline-demo",
        task_class="coding",
        complexity="medium",
        signals={},
        score=0.0,
        threshold=0.55,
        escalated=True,
        candidate_supply=supply,
        failure_kind="generation",
    ).to_dict()
    escalation = decide(FrontierSignals(candidate_supply=supply))
    check(
        record["candidate_supply"] == 0.0
        and record["failure_kind"] == "generation"
        and escalation.escalate
        and escalation.contributions[0][0] == "candidate_supply_shortfall",
        "supply=0 is a generation failure and fires frontier generation escalation",
    )


def test_selector_v2_rejects_artifact_cluster_manipulation() -> None:
    honest_diff = "@@ -10 +10 @@\n-return value\n+return value.strip()\n"
    honest = [candidate(label, honest_diff) for label in ("honest-a", "honest-b")]
    attacker = Candidate(
        "aaa-attacker",
        {"src/parser.py": "@@ -1 +1 @@\n-return value\n+steal consensus\n"},
        hard_results={"verified": True, "attacker_claim": True},
    )
    calls = 0

    def forbidden(_first: Candidate, _second: Candidate) -> Verdict:
        nonlocal calls
        calls += 1
        raise AssertionError("honest majority must resolve without judging")

    selection = select_v2(CONTRACT, (attacker, *honest), forbidden)
    check(
        selection.selected in {"honest-a", "honest-b"}
        and selection.comparisons == 0
        and calls == 0,
        f"fake hunk content and extra hard keys cannot beat honest consensus ({selection})",
    )


if __name__ == "__main__":
    test_selector_v2_offline_demo()
    test_selector_v2_rejects_artifact_cluster_manipulation()
    if FAILURES:
        print(f"FAIL selector v2 offline demo: {len(FAILURES)} checks failed")
        raise SystemExit(1)
    print("ok   selector v2 offline demo complete")
