#!/usr/bin/env python3
"""Frontier escalation: the mechanism, and the dataset it is waiting on.

The score is deliberately a rule-based weighted sum of observable signals with
guessed coefficients. What matters at this stage is not that the coefficients
are right - they are a starting guess - but that the mechanism is honest about
it: every escalation decision and its outcome is recorded, nothing is trained
on anything, and the summary says how much of the dataset is still unlabelled
rather than computing a false-positive rate over a handful of rows.

The bindings are pinned here too. The escalation path stays open through a
standby binding, and the inactive catalog candidate stays out of every active
binding and fallback list until its own activation gate.
"""

from __future__ import annotations

from _harness import setup_environment

from _harness import make_check

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import json  # noqa: E402
import tomllib  # noqa: E402

from grokbuild.datasets import (  # noqa: E402
    ATTRIBUTION_SCHEMA,
    ESCALATION_SCHEMA,
    JUDGE_CALIBRATION_SCHEMA,
    VERIFICATION_PLAN_SCHEMA,
    AttributionRecord,
    EscalationRecord,
    JudgeCalibrationRecord,
    VerificationPlanRecord,
    ProviderIdentityRefused,
    dataset_path,
    escalation_summary,
    read_records,
    record_attribution,
    record_escalation,
    record_judge_calibration,
    record_verification_plan,
)
from grokbuild.frontier import (  # noqa: E402
    DEFAULT_THRESHOLD,
    SIGNAL_WEIGHTS,
    FrontierSignals,
    decide,
    frontier_score,
)

FAILURES: list[str] = []
INACTIVE_CANDIDATES = ("m3-pro", "m3_pro")
EXCLUDED_VENDORS = ("claude-api",)


check = make_check(FAILURES)


def _config(name: str = "config.toml") -> dict:
    return tomllib.loads((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))


def test_a_quiet_task_is_never_escalated() -> None:
    decision = decide(FrontierSignals())
    check(decision.score == 0.0, f"no signal, no score ({decision.score})")
    check(not decision.escalate, "nothing to escalate")


def test_candidate_supply_is_neutral_until_observed() -> None:
    baseline = decide(FrontierSignals(planner_disagreement=1.0))
    absent = decide(FrontierSignals(planner_disagreement=1.0, candidate_supply=None))
    check(absent == baseline, "absent supply data leaves the legacy score byte-for-byte neutral")

    exhausted = decide(FrontierSignals(candidate_supply=0.0))
    check(exhausted.escalate, f"zero supply escalates toward generation ({exhausted.score})")
    check(
        exhausted.contributions[0][0] == "candidate_supply_shortfall",
        f"the generation shortfall is attributed explicitly ({exhausted.contributions[0]})",
    )
    check(
        not decide(FrontierSignals(candidate_supply=1.0)).escalate,
        "a portfolio with acceptable supply adds no escalation pressure",
    )


def test_disagreement_dominates_the_score() -> None:
    """Independent planners disagreeing is the strongest observable signal."""
    disagreement = decide(FrontierSignals(planner_disagreement=1.0))
    novelty = decide(FrontierSignals(novelty=1.0))
    check(
        disagreement.score > novelty.score,
        f"disagreement outweighs novelty ({disagreement.score} vs {novelty.score})",
    )
    top = disagreement.contributions[0]
    check(top[0] == "planner_disagreement", f"the report names what drove it ({top})")


def test_the_score_is_bounded_and_normalised() -> None:
    everything = FrontierSignals(**{name: 1.0 for name in SIGNAL_WEIGHTS})
    check(round(frontier_score(everything), 6) == 1.0, "every signal at once scores one")
    check(
        round(frontier_score(FrontierSignals(planner_disagreement=5.0)), 6)
        == round(frontier_score(FrontierSignals(planner_disagreement=1.0)), 6),
        "out-of-range input is clamped rather than trusted",
    )
    check(frontier_score(FrontierSignals(novelty=-3.0)) == 0.0, "negative input is clamped")


def test_escalation_needs_more_than_one_weak_signal() -> None:
    """Buying the expensive model on a single weak hint defeats the economics."""
    weak = decide(FrontierSignals(novelty=0.5))
    check(not weak.escalate, f"one weak signal is not enough ({weak.score})")
    strong = decide(
        FrontierSignals(
            planner_disagreement=1.0,
            verification_uncertainty=1.0,
            hypothesis_conflict=1.0,
            repair_history=1.0,
        )
    )
    check(strong.escalate, f"several strong signals do escalate ({strong.score})")
    check(strong.threshold == DEFAULT_THRESHOLD, "the threshold is reported with the decision")


def test_contributions_explain_a_surprising_escalation() -> None:
    decision = decide(FrontierSignals(planner_disagreement=1.0, novelty=1.0))
    check(len(decision.contributions) == len(SIGNAL_WEIGHTS), "every signal is accounted for")
    check(
        round(sum(value for _name, value in decision.contributions), 6) == round(decision.score, 6),
        "the contributions sum to the score",
    )
    payload = decision.to_dict()
    check(payload["escalate"] is False or payload["escalate"] is True, "the decision serializes")
    check(payload["contract_version"] == 1, "records carry their contract version")


def test_escalation_records_are_collected_not_trained_on(tmp: Path) -> None:
    setup_environment(tmp)
    signals = FrontierSignals(planner_disagreement=0.9, verification_uncertainty=0.8)
    decision = decide(signals)
    record_escalation(
        EscalationRecord(
            decision_id="d-1",
            task_class="coding",
            complexity="high",
            signals=signals.to_dict(),
            score=decision.score,
            threshold=decision.threshold,
            escalated=decision.escalate,
        )
    )
    stored = list(read_records(ESCALATION_SCHEMA))
    check(len(stored) == 1, f"one record was written ({len(stored)})")
    check(stored[0]["outcome"] == "unknown", "the outcome is not yet known")
    check(
        stored[0]["candidate_supply"] is None,
        "existing records default to unknown candidate supply",
    )
    check(stored[0]["failure_kind"] is None, "existing records default to no attribution")
    summary = escalation_summary()
    check(
        summary["status"] == "awaiting escalation dataset",
        f"the summary refuses to pretend it has enough data ({summary})",
    )
    check(summary["records"] == 1 and summary["labelled"] == 0, f"counts are honest ({summary})")


def test_a_record_refuses_provider_identity(tmp: Path) -> None:
    """Recording who produced a result would teach a router to prefer a brand."""
    setup_environment(tmp)
    from grokbuild.datasets import _reject_identity

    for payload in (
        {"model": "grok-4.6"},
        {"provider": "xai"},
        {"outcome": {"vendor": "anything"}},
        {"candidates": [{"agent": "someone"}]},
    ):
        try:
            _reject_identity(payload)
        except ProviderIdentityRefused as exc:
            check(True, f"identity is refused at the boundary ({exc})")
        else:
            check(False, f"identity slipped through ({payload})")
    try:
        _reject_identity({"signals": {"novelty": 1.0}, "candidates": ["A", "B"]})
    except ProviderIdentityRefused:
        check(False, "a clean record was refused")
    else:
        check(True, "a record without identity is accepted")


def test_judge_calibration_records_labels_not_vendors(tmp: Path) -> None:
    setup_environment(tmp)
    record_judge_calibration(
        JudgeCalibrationRecord(
            task_id="t-1",
            candidates=("A", "B"),
            verdict="a",
            reason_codes=("judges_agree",),
            accepted="A",
            later_regression=False,
        )
    )
    stored = list(read_records(JUDGE_CALIBRATION_SCHEMA))
    check(len(stored) == 1, "one calibration record")
    check(stored[0]["candidates"] == ["A", "B"], "candidates are anonymous labels")
    check(
        "model" not in json.dumps(stored[0]) and "provider" not in json.dumps(stored[0]),
        f"no vendor identity survives into the dataset ({stored[0]})",
    )
    check(
        dataset_path(JUDGE_CALIBRATION_SCHEMA).name == "judge-calibration-v1.jsonl",
        "the dataset is versioned in its own file",
    )


def test_dataset_records_are_bounded(tmp: Path) -> None:
    setup_environment(tmp)
    record_judge_calibration(
        JudgeCalibrationRecord(
            task_id="t-1",
            candidates=tuple(f"C{index}" for index in range(500)),
            verdict="a",
            human_correction="x" * 5000,
        )
    )
    stored = list(read_records(JUDGE_CALIBRATION_SCHEMA))[0]
    check(
        len(stored["candidates"]) <= 32, f"candidate lists are capped ({len(stored['candidates'])})"
    )
    check(len(stored["human_correction"]) <= 200, "free text is capped")


def test_verification_plan_records_round_trip_and_are_collection_only(tmp: Path) -> None:
    setup_environment(tmp)
    record_verification_plan(
        VerificationPlanRecord(
            task_id="t-1",
            candidate_labels=("A", "B"),
            kind="discriminating_test",
            hypothesis="the check separates the candidates",
            constraints=("same input",),
            requirements=("build passes",),
            checks=("pytest",),
            affected_path_count=2,
            invariant_tags=("anonymous",),
            decision_id="d-1",
        )
    )
    stored = list(read_records(VERIFICATION_PLAN_SCHEMA))
    check(len(stored) == 1, "one verification plan record")
    check(stored[0]["schema"] == VERIFICATION_PLAN_SCHEMA, "verification schema is exact")
    check(
        dataset_path(VERIFICATION_PLAN_SCHEMA).name == "verification-plan-v1.jsonl",
        "verification plans use a versioned filename",
    )


def test_attribution_records_round_trip_and_are_collection_only(tmp: Path) -> None:
    setup_environment(tmp)
    record_attribution(
        AttributionRecord(
            task_id="t-1",
            baseline_label="A",
            candidate_label="B",
            outcome="attributed",
            affected_behavior_tags=("latency",),
            discriminating_check="pytest",
            decision_id="d-1",
        )
    )
    stored = list(read_records(ATTRIBUTION_SCHEMA))
    check(len(stored) == 1, "one attribution record")
    check(stored[0]["schema"] == ATTRIBUTION_SCHEMA, "attribution schema is exact")
    check(
        dataset_path(ATTRIBUTION_SCHEMA).name == "attribution-v1.jsonl",
        "attributions use a versioned filename",
    )


def test_new_dataset_records_refuse_provider_identity_and_bound_fields(tmp: Path) -> None:
    setup_environment(tmp)
    records = (
        VerificationPlanRecord(
            task_id="t-1",
            candidate_labels=({"model": "hidden"},),
            kind="kind",
            hypothesis="hypothesis",
        ),
        AttributionRecord(
            task_id="t-1",
            baseline_label="A",
            candidate_label="B",
            outcome="inconclusive",
            affected_behavior_tags=({"provider": "hidden"},),
        ),
    )
    for record in records:
        try:
            record.to_dict()
        except ProviderIdentityRefused as exc:
            check(True, f"nested provider identity is refused ({exc})")
        else:
            check(False, "nested provider identity was accepted")
    clean = VerificationPlanRecord(
        task_id="t-1",
        candidate_labels=("A", "B"),
        kind="k",
        hypothesis="h",
        affected_path_count=99,
        checks=tuple("x" * 500 for _ in range(99)),
    ).to_dict()
    check(
        "model" not in json.dumps(clean) and "provider" not in json.dumps(clean),
        "clean plans stay anonymous",
    )
    check(len(clean["checks"]) <= 32 and len(clean["checks"][0]) <= 200, "plan fields are bounded")


def test_criterion_judge_binding_has_no_excluded_vendor_string() -> None:
    for name in ("config.toml", "config.example.toml"):
        spec = _config(name)["subagents"]["roles"]["criterion-judge"]
        text = json.dumps(spec).casefold()
        for vendor in EXCLUDED_VENDORS:
            check(vendor not in text, f"criterion-judge on {name} excludes {vendor!r}")
    defaults = json.loads(
        (REPO_ROOT / "grokbuild" / "roles_default.json").read_text(encoding="utf-8")
    )
    text = json.dumps(defaults["criterion-judge"]).casefold()
    for vendor in EXCLUDED_VENDORS:
        check(vendor not in text, f"criterion-judge defaults exclude {vendor!r}")


def test_frontier_binding_keeps_the_escalation_path_open() -> None:
    roles = _config()["subagents"]["roles"]
    check("frontier-resolver" in roles, "the frontier role is bound")
    check(
        roles["frontier-resolver"]["model"] == "grok-4.6",
        f"the primary binding is the strongest available reasoning model ({roles['frontier-resolver']})",
    )
    fallback = roles["frontier-resolver"].get("fallback", [])
    check(fallback == ["frontier-resolver-standby"], f"one standby binding ({fallback})")
    check(
        roles["frontier-resolver-standby"]["model"] == "gpt-5.6-sol",
        "the standby is a different provider family",
    )


def test_the_inactive_candidate_is_in_no_active_binding() -> None:
    """A model that has not shipped may not silently become a live binding."""
    for name in ("config.toml", "config.example.toml"):
        roles = _config(name)["subagents"]["roles"]
        for role, spec in roles.items():
            model = str(spec.get("model", "")).casefold()
            for candidate in INACTIVE_CANDIDATES:
                check(
                    candidate not in model,
                    f"{name}:{role} does not bind the inactive candidate ({model})",
                )
            for fallback in spec.get("fallback", []):
                target = str(roles.get(fallback, {}).get("model", "")).casefold()
                for candidate in INACTIVE_CANDIDATES:
                    check(
                        candidate not in target,
                        f"{name}:{role} does not fall back to the inactive candidate",
                    )


def test_the_excluded_vendor_is_absent_from_the_pool() -> None:
    surfaces = [
        (REPO_ROOT / "config" / "config.toml").read_text(encoding="utf-8"),
        (REPO_ROOT / "config" / "config.example.toml").read_text(encoding="utf-8"),
        (REPO_ROOT / "grokbuild" / "providers.json").read_text(encoding="utf-8"),
        (REPO_ROOT / "grokbuild" / "roles_default.json").read_text(encoding="utf-8"),
    ]
    for text in surfaces:
        lowered = text.casefold()
        for vendor in EXCLUDED_VENDORS:
            check(vendor not in lowered, f"the excluded vendor {vendor!r} is absent")


def test_no_learned_router_ships_yet() -> None:
    """The mechanism collects; fitting a policy on an empty dataset would be noise."""
    source = (REPO_ROOT / "grokbuild" / "frontier.py").read_text(encoding="utf-8")
    check(
        "AWAITING ESCALATION DATASET" in source,
        "the coefficients are marked as awaiting the dataset",
    )
    for forbidden in ("fit(", "train(", "sklearn", "numpy"):
        check(forbidden not in source, f"no training code ships ({forbidden})")


def test_escalation_stage_is_inert_by_default() -> None:
    from grokbuild.compose import compose_frontier_stage
    from grokbuild.policy import load_profiles

    profiles = load_profiles()
    hot = decide(
        FrontierSignals(
            planner_disagreement=1.0,
            verification_uncertainty=1.0,
            hypothesis_conflict=1.0,
            repair_history=1.0,
        )
    )
    check(
        profiles["default"].frontier_escalation is None,
        "the default profile does not escalate",
    )
    check(
        "frontier_escalation" not in profiles["default"].to_dict(),
        "an unset flag is absent from the serialized profile",
    )
    check(
        compose_frontier_stage(profiles["default"], hot) == [],
        "even a hot score composes nothing by default",
    )


def test_pairwise_abstention_requests_evidence_before_frontier_adjudication() -> None:
    from grokbuild.artifact_judge import Verdict
    from grokbuild.compose import compose_adjudication_stage, compose_evidence_stage
    from grokbuild.verifier_planner import analyze

    request = analyze(Verdict("needs_discriminating_test", reason_codes=("low_margin",)), ("A", "B"))
    check(request is not None, "an aggregate abstention requests discriminating evidence")
    evidence = compose_evidence_stage(request)
    adjudication = compose_adjudication_stage("judge-frontier-code")
    check(evidence.kind == "evidence_collection", "evidence collection precedes adjudication")
    check(adjudication.kind == "judge", "frontier adjudication remains the unresolved tail")


def test_escalation_stage_names_what_drove_it() -> None:
    from grokbuild.compose import compose_frontier_stage
    from grokbuild.policy import load_profiles

    profile = load_profiles()["frontier"]
    quiet = decide(FrontierSignals(novelty=0.2))
    check(compose_frontier_stage(profile, quiet) == [], "a quiet task is not escalated")
    hot = decide(
        FrontierSignals(
            planner_disagreement=1.0,
            verification_uncertainty=1.0,
            hypothesis_conflict=1.0,
            repair_history=1.0,
        )
    )
    stages = compose_frontier_stage(profile, hot)
    check(len(stages) == 1, f"one meta-planning stage ({len(stages)})")
    stage = stages[0]
    check(stage.role == "frontier-resolver", f"the frontier role is used ({stage.role})")
    check(
        stage.alternatives == ("frontier-resolver-standby",),
        f"the standby binding is the declared alternative ({stage.alternatives})",
    )
    check(
        "planner_disagreement" in stage.reason and "crossed" in stage.reason,
        f"the reason names the signal and the threshold ({stage.reason})",
    )
