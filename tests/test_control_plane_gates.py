"""Stage-12 gate regressions from the 2026-09-05 audit (findings B8-B13).

Each test is the proof-of-concept that bypassed a gate of the dormant
control plane, and now must fail closed before the judge/portfolio layer is
switched on.
"""

from __future__ import annotations

from grokbuild import verifier_planner
from grokbuild.artifact_judge import (
    REASON_CODES,
    Candidate,
    JudgeOpinion,
    Requirement,
    TaskContract,
    compare,
    fold_opinions,
    sanitize,
)
from grokbuild.selector import Selection, cluster_artifacts, select_v2
from grokbuild.task_evidence import TaskResult, TaskSpec
from grokbuild.task_verify import Observation, verify

CONTRACT = TaskContract("task-1", "fix the parser", (Requirement("tests_pass", hard=True),))


def _candidate(label: str, source: str, **overrides) -> Candidate:
    spec = {
        "artifacts": {"src/parser.py": source},
        "evidence_refs": (f"evidence:{label}",),
        "hard_results": {"tests_pass": True},
    }
    spec.update(overrides)
    return Candidate(label, **spec)


def _spec(**overrides) -> TaskSpec:
    fields = {"decision_id": "d-1", "stage_key": "impl", "role": "implement-standard"}
    fields.update(overrides)
    return TaskSpec(**fields)


def _result(**overrides) -> TaskResult:
    fields = {"decision_id": "d-1", "stage_key": "impl", "status": "success"}
    fields.update(overrides)
    return TaskResult(**fields)


# --- B8: self-declared criteria and untouched artifacts are not claims -------


def test_result_criteria_the_contract_never_asked_for_do_not_verify() -> None:
    result = _result(criteria=({"kind": "check_passed", "observed": "pytest"},))
    verdict = verify(_spec(), result, Observation())
    assert not verdict.verified
    assert "criteria_unverifiable" in verdict.reason_codes
    assert "result_malformed" in verdict.reason_codes


def test_an_existing_but_untouched_artifact_is_not_a_claim() -> None:
    result = _result(artifacts=("README.md",))
    verdict = verify(_spec(), result, Observation(existing_paths=frozenset({"README.md"})))
    assert not verdict.verified and "result_malformed" in verdict.reason_codes


def test_a_produced_artifact_is_a_claim() -> None:
    result = _result(artifacts=("dist/report.md",))
    observation = Observation(
        existing_paths=frozenset({"dist/report.md"}), changed_paths=("dist/report.md",)
    )
    assert verify(_spec(), result, observation).verified


def test_a_result_criterion_restating_the_contract_still_verifies() -> None:
    spec = _spec(criteria=({"kind": "check_passed", "expected": "verify/0"},))
    result = _result(criteria=({"kind": "check_passed", "observed": "verify/0"},))
    assert verify(spec, result, Observation(checks={"verify/0": True})).verified
    assert not verify(spec, result, Observation(checks={})).verified


# --- B9: consensus clustering keeps block structure --------------------------


OUTSIDE_IF = "for x in xs:\n    if x:\n        y()\n    z()\n"
INSIDE_IF = "for x in xs:\n    if x:\n        y()\n        z()\n"


def test_indentation_levels_separate_clusters() -> None:
    clusters = cluster_artifacts(
        (
            _candidate("attacker-1", INSIDE_IF),
            _candidate("attacker-2", INSIDE_IF),
            _candidate("honest", OUTSIDE_IF),
        )
    )
    assert {cluster.members for cluster in clusters} == {("attacker-1", "attacker-2"), ("honest",)}


def test_consistent_reindentation_remains_byte_distinct() -> None:
    two_space = "for x in xs:\n  if x:\n    y()\n  z()\n"
    tabs = "for x in xs:\n\tif x:\n\t\ty()\n\tz()\n"
    clusters = cluster_artifacts(
        (_candidate("a", OUTSIDE_IF), _candidate("b", two_space), _candidate("c", tabs))
    )
    assert {cluster.members for cluster in clusters} == {("a",), ("b",), ("c",)}


def test_select_v2_no_longer_hands_consensus_to_a_reindented_copy() -> None:
    def judge(_first: Candidate, _second: Candidate):
        raise AssertionError("consensus must not be reached without judging")

    honest = _candidate("honest", OUTSIDE_IF)
    attackers = (_candidate("aaa-1", INSIDE_IF), _candidate("aaa-2", INSIDE_IF))
    selection: Selection = select_v2(CONTRACT, (honest, *attackers), judge)
    assert selection.selected in {"aaa-1", "aaa-2"}  # a real 2-1 majority still wins
    clusters = cluster_artifacts((honest, *attackers))
    assert len(clusters) == 2


# --- B10: identity scan on word boundaries -----------------------------------


def test_component_aliases_only_match_whole_words() -> None:
    _bundle, leaks = sanitize(
        Candidate("A", {"grokbuild/hook.py": "def handle(): ...\n"}), ("grok-4.6",)
    )
    assert leaks == ()
    _bundle, leaks = sanitize(Candidate("A", {"notes.txt": "lunar_phase = 3\n"}), ("gpt-5.6-luna",))
    assert leaks == ()
    _bundle, leaks = sanitize(
        Candidate("A", {"notes.txt": "the flash and pro and max settings\n"}),
        ("deepseek-v4-pro", "glm-5.3-flash", "qwen3.8-max"),
    )
    assert leaks == ()


def test_real_identity_mentions_still_leak() -> None:
    _bundle, leaks = sanitize(
        Candidate("A", {"notes.txt": "tuned on gpt-5.6-luna\n"}), ("gpt-5.6-luna",)
    )
    assert leaks and "gpt-5.6-luna" in leaks[0]
    _bundle, leaks = sanitize(
        Candidate("A", {"notes.txt": "model: GPT 5.6 Luna\n"}), ("gpt-5.6-luna",)
    )
    assert leaks
    _bundle, leaks = sanitize(
        Candidate("A", {"notes.txt": "via g-p-t-5.6-l-u-n-a\n"}), ("gpt-5.6-luna",)
    )
    assert leaks, "separator evasion of a long term is still caught"
    _bundle, leaks = sanitize(
        Candidate("A", {"notes.txt": "built on luna today\n"}), ("gpt-5.6-luna",)
    )
    assert leaks, "a component alias as its own word is identity"
    _bundle, leaks = sanitize(Candidate("A", {"notes.txt": "import grok\n"}), ("grok-4.6",))
    assert leaks


# --- B11: reversed-order judging is required ---------------------------------


def test_a_single_opinion_is_not_agreement() -> None:
    a, b = _candidate("A", OUTSIDE_IF), _candidate("B", INSIDE_IF)
    verdict, confidence, codes = fold_opinions(a, b, (JudgeOpinion("A", "B", "A", 0.9),))
    assert verdict == "needs_discriminating_test" and confidence == 0.0
    assert codes == ("order_not_reversed",) and "order_not_reversed" in REASON_CODES


def test_two_opinions_in_the_same_order_are_not_agreement() -> None:
    a, b = _candidate("A", OUTSIDE_IF), _candidate("B", INSIDE_IF)
    opinions = (JudgeOpinion("A", "B", "A", 0.9), JudgeOpinion("A", "B", "A", 0.8))
    assert fold_opinions(a, b, opinions)[0] == "needs_discriminating_test"
    assert compare(CONTRACT, a, b, opinions).verdict == "needs_discriminating_test"


def test_reversed_order_agreement_remains_a_preference() -> None:
    a, b = _candidate("A", OUTSIDE_IF), _candidate("B", INSIDE_IF)
    opinions = (JudgeOpinion("A", "B", "B", 0.9), JudgeOpinion("B", "A", "B", 0.7))
    assert fold_opinions(a, b, opinions) == ("b", 0.7, ("judges_agree",))


def test_an_opinion_about_another_pair_is_incomplete() -> None:
    a, b = _candidate("A", OUTSIDE_IF), _candidate("B", INSIDE_IF)
    opinions = (JudgeOpinion("A", "B", "A", 0.9), JudgeOpinion("C", "A", "A", 0.9))
    assert fold_opinions(a, b, opinions)[2] == ("bundle_incomplete",)


# --- B12: the bundle frame is real text --------------------------------------


def test_bundle_frames_artifacts_with_real_newlines_and_untouched_code() -> None:
    bundle, leaks = sanitize(Candidate("A", {"src/cmp.py": "if a < b and c > d:\n    pass\n"}))
    assert leaks == ()
    _path, rendered = bundle.artifacts[0]
    assert rendered.startswith("[ARTIFACT path=src/cmp.py]\n")
    assert rendered.endswith("\n[/ARTIFACT]")
    assert "if a < b and c > d:" in rendered
    assert "\\n" not in rendered and "u003c" not in rendered


# --- B13: verifier-planner tags need a real word ------------------------------


def test_short_stems_produce_no_tags_and_tags_match_whole_words() -> None:
    inventory = ("checks/a-suite", "tests/test_parser.py", "checks/parsers-all", "checks/state")
    verdict = compare(CONTRACT, _candidate("A", OUTSIDE_IF), _candidate("B", INSIDE_IF), ())
    short = verifier_planner.analyze(
        verdict,
        ("A", "B"),
        verifier_planner.ChangeMap(affected_paths=("a.py", "ab.py"), check_inventory=inventory),
    )
    assert short.checks == ()
    exact = verifier_planner.analyze(
        verdict,
        ("A", "B"),
        verifier_planner.ChangeMap(affected_paths=("src/parser.py",), check_inventory=inventory),
    )
    assert exact.checks == ("tests/test_parser.py",)
    outcome = verifier_planner.attribute_evidence(
        {"checks": {"checks/a-suite": False, "checks/state": True}},
        {"checks": {"checks/a-suite": True, "checks/state": True}},
        verifier_planner.ChangeMap(affected_paths=("a.py",)),
    )
    assert outcome == "inconclusive"
