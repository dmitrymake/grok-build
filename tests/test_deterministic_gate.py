from grokbuild.deterministic_gate import GateCheck, decide_gate
from grokbuild.artifact_judge import Candidate, JudgeOpinion, Requirement, TaskContract, compare


def test_any_explicit_failure_rejects_candidate_against_unanimous_judges():
    contract = TaskContract("t", requirements=(Requirement("tests"),))
    passing = Candidate("A", {"x": "ok"}, hard_results={"tests": True})
    failing = Candidate(
        "B",
        {"x": "plausible"},
        hard_results={"tests": True},
        gate_checks=(GateCheck("scanner", "security", "FAIL", "scan:1"),),
    )
    verdict = compare(
        contract,
        passing,
        failing,
        (JudgeOpinion("A", "B", "B", 1), JudgeOpinion("B", "A", "B", 1)),
    )
    assert verdict.verdict == "a"
    assert verdict.rejected_candidates == ("B",)
    assert "deterministic_reject" in verdict.reason_codes


def test_shared_fail_rejects_all_and_unknown_abstains():
    failed = decide_gate(
        {
            "A": (GateCheck("schema", "response", "FAIL"),),
            "B": (GateCheck("tests", "suite", "FAIL"),),
        }
    )
    assert failed.outcome == "reject_all" and failed.eligible == ()
    unknown = decide_gate({"A": (GateCheck("invariant", "state", "UNKNOWN"),)})
    assert unknown.outcome == "abstain" and unknown.unknown == ("A",)
