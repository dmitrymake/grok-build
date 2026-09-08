#!/usr/bin/env python3
"""R2 typed evidence: contracts, independent verification, completion seam.

Completion used to mean "a terminal non-empty result exists". These tests pin
the replacement: an immutable spec is attached before the child spawns, the
child's typed result is only a claim, and the runtime decides completion from
its own observation of the repository. The strict rule is gated behind a
profile, so the legacy behaviour these fixtures also cover stays untouched by
default.
"""

from __future__ import annotations

from _harness import setup_environment

from _harness import plant_session

from _harness import run_hook_main

from _harness import make_check

from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()

import json  # noqa: E402
import os  # noqa: E402

from grokbuild import persist  # noqa: E402
from grokbuild.policy import load_profiles  # noqa: E402
from grokbuild.state import default_state_path
from grokbuild.task_evidence import (  # noqa: E402
    TASK_EVIDENCE_SCHEMA,
    TaskResult,
    TaskSpec,
    attach_task_spec,
    decide_completion,
    find_task_spec,
    observe,
    parse_task_result,
    task_evidence_path,
    verify_task_result,
)
from grokbuild.task_verify import Observation, Verification, verify  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "r2"
from _harness import SID

FAILURES: list[str] = []


check = make_check(FAILURES)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _spec() -> TaskSpec:
    spec = TaskSpec.from_dict(_fixture("spec.json"))
    assert spec is not None
    return spec


def _result(name: str) -> TaskResult | None:
    return TaskResult.from_dict(_fixture(name))


def _observation(**overrides) -> Observation:
    defaults = {
        "existing_paths": frozenset({"src/app.py"}),
        "changed_paths": ("src/app.py",),
        "checks": {"verify/0": True},
    }
    defaults.update(overrides)
    return Observation(**defaults)


def test_verified_result_completes(tmp: Path) -> None:
    """The one shape that may complete a stage: every claim confirmed."""
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-success.json"), _observation())
    check(verdict.verified, f"a fully confirmed result verifies ({verdict.failures})")
    check(verdict.reason_codes == ("verified",), f"reason codes ({verdict.reason_codes})")


def test_missing_result_is_evidence_failure(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), None, _observation())
    check(
        not verdict.verified and verdict.reason_codes == ("result_missing",),
        f"a terminal result with no typed payload cannot complete ({verdict})",
    )


def test_malformed_result_is_rejected_by_the_parser(tmp: Path) -> None:
    setup_environment(tmp)
    check(
        TaskResult.from_dict(_fixture("result-malformed.json")) is None,
        "a foreign schema is not a task result",
    )


def test_empty_result_does_not_complete(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-empty.json"), _observation())
    check(not verdict.verified, "an empty result never completes")
    check(
        "status_not_success" in verdict.reason_codes
        and "diff_undeclared_change" in verdict.reason_codes,
        f"the empty result is called out precisely ({verdict.reason_codes})",
    )


def test_result_outside_the_path_scope_does_not_complete(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(
        _spec(),
        _result("result-wrong-path.json"),
        _observation(changed_paths=("src/app.py", "/etc/passwd")),
    )
    check(not verdict.verified, "an out-of-scope path never completes")
    check(
        "path_outside_scope" in verdict.reason_codes,
        f"the scope violation is named ({verdict.reason_codes})",
    )


def test_observed_out_of_scope_changes_are_named(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(
        _spec(),
        _result("result-success.json"),
        _observation(changed_paths=("src/app.py", ".grok/x")),
    )
    check(not verdict.verified, "an observed out-of-scope change never completes")
    check(
        "observed_path_outside_scope" in verdict.reason_codes,
        f"the observed scope violation is named ({verdict.reason_codes})",
    )


def test_observed_out_of_scope_changes_are_allowed_without_scope(tmp: Path) -> None:
    setup_environment(tmp)
    spec = replace(_spec(), path_scope=())
    verdict = verify(
        spec,
        _result("result-success.json"),
        _observation(changed_paths=("src/app.py", ".grok/x")),
    )
    check(
        "observed_path_outside_scope" not in verdict.reason_codes,
        f"empty scope remains unrestricted ({verdict.reason_codes})",
    )


def test_declared_traversal_is_outside_scope(tmp: Path) -> None:
    setup_environment(tmp)
    result = replace(_result("result-success.json"), changed_paths=("src/../.grok/x",))
    verdict = verify(_spec(), result, _observation(changed_paths=("src/../.grok/x",)))
    check(not verdict.verified, "a traversal path never completes")
    check(
        "path_outside_scope" in verdict.reason_codes,
        f"declared traversal is named as outside scope ({verdict.reason_codes})",
    )


def test_mismatched_diff_does_not_complete(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-mismatched-diff.json"), _observation())
    check(not verdict.verified, "a diff that disagrees with the claim never completes")
    check(
        "diff_missing_declared" in verdict.reason_codes
        and "diff_undeclared_change" in verdict.reason_codes,
        f"both directions of the mismatch are reported ({verdict.reason_codes})",
    )


def test_unresolved_items_do_not_complete(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-unresolved.json"), _observation())
    check(
        not verdict.verified and "unresolved_items" in verdict.reason_codes,
        f"unresolved work blocks completion ({verdict.reason_codes})",
    )


def test_a_check_the_runtime_never_ran_is_not_a_pass(tmp: Path) -> None:
    """The child's word that a check passed is not evidence that it ran."""
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-success.json"), _observation(checks={}))
    check(
        not verdict.verified and "check_not_run" in verdict.reason_codes,
        f"a declared-but-unrun check fails closed ({verdict.reason_codes})",
    )


def test_self_attested_check_without_observation_does_not_verify(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-success.json"), Observation())
    check(
        not verdict.verified and "check_not_run" in verdict.reason_codes,
        f"an empty observation cannot confirm a self-attested check ({verdict})",
    )


def test_result_check_absent_from_runtime_does_not_verify(tmp: Path) -> None:
    setup_environment(tmp)
    result = _result("result-success.json")
    assert result is not None
    result = replace(result, checks=({"name": "other-check", "passed": True},))
    verdict = verify(_spec(), result, _observation())
    check(
        not verdict.verified and "check_not_run" in verdict.reason_codes,
        f"an unobserved result check fails closed ({verdict.reason_codes})",
    )


def test_result_passing_check_contradicted_by_runtime_does_not_verify(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(
        _spec(), _result("result-success.json"), _observation(checks={"verify/0": False})
    )
    check(
        not verdict.verified and "check_failed" in verdict.reason_codes,
        f"a runtime failure defeats a passing claim ({verdict.reason_codes})",
    )


def test_result_artifact_absent_from_observation_does_not_verify(tmp: Path) -> None:
    setup_environment(tmp)
    result = _result("result-success.json")
    assert result is not None
    result = replace(result, artifacts=("src/missing.py",))
    verdict = verify(_spec(), result, _observation())
    check(
        not verdict.verified and "artifact_missing" in verdict.reason_codes,
        f"an unobserved result artifact fails closed ({verdict.reason_codes})",
    )


def test_fully_corroborated_result_still_verifies(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(_spec(), _result("result-success.json"), _observation())
    check(verdict.verified, f"runtime corroboration still permits verification ({verdict})")


def test_a_missing_artifact_is_not_a_pass(tmp: Path) -> None:
    setup_environment(tmp)
    verdict = verify(
        _spec(), _result("result-success.json"), _observation(existing_paths=frozenset())
    )
    check(
        not verdict.verified and "artifact_missing" in verdict.reason_codes,
        f"an absent artifact fails closed ({verdict.reason_codes})",
    )


def test_an_unsupported_criterion_kind_fails_closed(tmp: Path) -> None:
    setup_environment(tmp)
    spec = _spec()
    widened = TaskSpec(
        decision_id=spec.decision_id,
        stage_key=spec.stage_key,
        role=spec.role,
        session_id=spec.session_id,
        path_scope=spec.path_scope,
        artifacts=spec.artifacts,
        checks=spec.checks,
        criteria=({"kind": "vibes", "expected": "looks right"},),
    )
    verdict = verify(widened, _result("result-success.json"), _observation())
    check(
        not verdict.verified and "criteria_unverifiable" in verdict.reason_codes,
        f"a criterion the runtime cannot confirm is not confirmed ({verdict.reason_codes})",
    )


def test_spec_attachment_is_immutable(tmp: Path) -> None:
    """A later spawn for the same stage cannot widen the contract."""
    setup_environment(tmp)
    first = attach_task_spec(
        TaskSpec(decision_id="d-1", stage_key="impl", role="implement-standard", checks=("a",))
    )
    second = attach_task_spec(
        TaskSpec(
            decision_id="d-1",
            stage_key="impl",
            role="implement-standard",
            checks=(),
            path_scope=("/",),
        )
    )
    check(second.checks == ("a",), f"the original contract stays in force ({second.checks})")
    check(second.created_at == first.created_at, "the original attachment timestamp is kept")
    stored = find_task_spec("d-1", "impl")
    check(stored is not None and stored.checks == ("a",), "the stored spec is the original")
    check(find_task_spec("d-1", "other") is None, "an unattached stage has no spec")


def test_open_spec_survives_spec_archive_rotation(tmp: Path) -> None:
    """The fallback retention policy carries contracts into fresh generations."""
    setup_environment(tmp)
    target = task_evidence_path()
    original = persist.MAX_LOG_BYTES
    persist.MAX_LOG_BYTES = 512
    try:
        for index in range(12):
            attach_task_spec(
                TaskSpec(
                    decision_id=f"rotation-{index}",
                    stage_key="impl",
                    role="implement-standard",
                    checks=(f"check-{index}",),
                )
            )
    finally:
        persist.MAX_LOG_BYTES = original
    check(
        find_task_spec("rotation-0", "impl", path=target) is not None,
        "an open contract survives repeated spec archive rotation",
    )
    check(
        find_task_spec("rotation-11", "impl", path=target) is not None,
        "the newest contract remains citable after rotation",
    )


def test_spec_archive_deduplicates_fallback_retention_records(tmp: Path) -> None:
    """Fallback retention keeps one latest record per contract identity."""
    setup_environment(tmp)
    target = task_evidence_path()
    attach_task_spec(TaskSpec(decision_id="duplicate", stage_key="impl", role="old"))
    archive = target.with_name(target.name + ".specs")
    persist.append_jsonl(
        archive,
        {
            "schema": "r2-task-spec-v1",
            "decision_id": "duplicate",
            "stage_key": "impl",
            "role": "old",
        },
    )
    original = persist.MAX_LOG_BYTES
    persist.MAX_LOG_BYTES = 1
    try:
        attach_task_spec(TaskSpec(decision_id="next", stage_key="impl", role="new"))
    finally:
        persist.MAX_LOG_BYTES = original
    identities = [
        (record["decision_id"], record["stage_key"])
        for record in (
            json.loads(line)
            for generation in (
                archive.with_name(archive.name + ".2"),
                archive.with_name(archive.name + ".1"),
                archive,
            )
            if generation.is_file()
            for line in generation.read_text(encoding="utf-8").splitlines()
        )
        if record.get("schema") == "r2-task-spec-v1"
    ]
    check(
        identities.count(("duplicate", "impl")) == 1,
        "fallback retention deduplicates a sole contract identity",
    )
    check(
        find_task_spec("duplicate", "impl", path=target) is not None,
        "the deduplicated contract remains citable",
    )


def test_typed_result_is_parsed_out_of_child_prose(tmp: Path) -> None:
    setup_environment(tmp)
    payload = json.dumps(_fixture("result-success.json"))
    fenced = f"Here is what I did.\n\n```json\n{payload}\n```\n\nDone."
    check(parse_task_result(fenced) is not None, "a fenced typed result is found in prose")
    check(parse_task_result(payload) is not None, "a bare typed result is found")
    check(parse_task_result("I finished the work.") is None, "prose alone carries no result")
    check(parse_task_result(None) is None, "no text carries no result")


def test_legacy_policy_records_without_enforcing(tmp: Path) -> None:
    """Under the legacy policy the answer stays exactly today's answer."""
    setup_environment(tmp)
    decision = decide_completion(
        policy=None,
        legacy_success=True,
        spec=_spec(),
        result=None,
        observation=Observation(),
    )
    check(decision.completes, "a legacy prose result still completes")
    check(
        decision.marker == "typed_evidence_missing",
        f"the missing typed payload is visible metadata ({decision.marker})",
    )
    check(
        decision.verification.reason_codes == ("not_evaluated",),
        f"no verdict is claimed without an observation ({decision.verification})",
    )


def test_strict_policy_requires_verified_evidence(tmp: Path) -> None:
    setup_environment(tmp)
    blocked = decide_completion(
        policy="strict",
        legacy_success=True,
        spec=_spec(),
        result=None,
        observation=_observation(),
    )
    check(not blocked.completes, "strict policy rejects a result with no typed payload")
    check(blocked.marker == "typed_evidence_missing", "the marker names why")

    allowed = decide_completion(
        policy="strict",
        legacy_success=False,
        spec=_spec(),
        result=_result("result-success.json"),
        observation=_observation(),
    )
    check(
        allowed.completes,
        f"strict policy accepts verified evidence even without legacy success ({allowed.verification})",
    )

    unattached = decide_completion(
        policy="strict",
        legacy_success=True,
        spec=None,
        result=_result("result-success.json"),
        observation=_observation(),
    )
    check(not unattached.completes, "strict policy needs a spec to verify against")


def test_default_profile_leaves_the_feature_inert(tmp: Path) -> None:
    setup_environment(tmp)
    profiles = load_profiles()
    check(
        profiles["default"].evidence_policy is None,
        "the default profile carries no evidence policy",
    )
    check(
        "evidence_policy" not in profiles["default"].to_dict(),
        "an unset policy is absent from the serialized profile",
    )
    check(
        profiles["evidence"].evidence_policy == "strict",
        "the opt-in profile carries the strict policy",
    )


def test_evidence_sidecar_is_versioned_and_separate(tmp: Path) -> None:
    """R2 records ride their own schema, never state v8 or the evidence-v1 log."""
    setup_environment(tmp)
    attach_task_spec(TaskSpec(decision_id="d-1", stage_key="impl", role="implement-standard"))
    path = task_evidence_path()
    check(path.name == "r2-evidence-v1.jsonl", f"dedicated sidecar path ({path.name})")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    check(len(records) == 1, f"one record per attachment ({len(records)})")
    check(
        records[0]["schema"] == "r2-task-spec-v1" and records[0]["contract_version"] == 1,
        f"records are versioned ({records[0].get('schema')})",
    )
    check(
        not (default_state_path().parent / "evidence-v1.jsonl").exists(),
        "the failure-cause sidecar is untouched",
    )


def test_observation_only_probes_named_paths(tmp: Path) -> None:
    setup_environment(tmp)
    root = tmp / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    observation = observe(_spec(), repo_root=root, changed_paths=("src/app.py",))
    check(
        observation.existing_paths == frozenset({"src/app.py"}),
        f"only declared paths are probed ({observation.existing_paths})",
    )
    verdict = verify_task_result(_spec(), _result("result-success.json"), observation)
    check(
        not verdict.verified and "check_not_run" in verdict.reason_codes,
        "an observation without check outcomes cannot confirm a check",
    )


def test_observation_rejects_unsafe_paths_and_normalizes_safe_paths(tmp: Path) -> None:
    setup_environment(tmp)
    root = tmp / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    observation = observe(
        _spec(),
        repo_root=root,
        changed_paths=("/etc/passwd", "src/../.grok/x", "src/lib/../app.py"),
    )
    check(
        "src/../.grok/x" not in observation.changed_paths
        and "src/app.py" in observation.changed_paths,
        f"paths are canonicalized before observation ({observation.changed_paths})",
    )
    check(
        "/etc/passwd" not in observation.existing_paths
        and "src/../.grok/x" not in observation.existing_paths,
        f"unsafe paths are not probed ({observation.existing_paths})",
    )


def test_declared_absolute_path_does_not_complete(tmp: Path) -> None:
    setup_environment(tmp)
    result = replace(_result("result-success.json"), changed_paths=("/etc/passwd",))
    verdict = verify(_spec(), result, _observation(changed_paths=("/etc/passwd",)))
    check(not verdict.verified, "an absolute declared path never completes")
    check(
        "path_outside_scope" in verdict.reason_codes,
        f"absolute declared paths are outside scope ({verdict.reason_codes})",
    )


def test_verification_serializes_for_the_dataset(tmp: Path) -> None:
    setup_environment(tmp)
    payload = Verification(False, ("check_failed",), ("required check failed: verify/0",)).to_dict()
    check(payload["verified"] is False, "the verdict serializes")
    check(payload["reason_codes"] == ["check_failed"], "reason codes serialize as a list")
    check(TASK_EVIDENCE_SCHEMA == "r2-evidence-v1", "the outcome schema name is stable")


def run_main(payload: dict) -> tuple[int, str]:
    return run_hook_main(payload, workspace_root=REPO_ROOT)


PAYLOAD_FIXTURES = REPO_ROOT / "grokbuild" / "fixtures" / "task-payloads"
TASK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _drive_linear_stage(tmp: Path) -> tuple[str, str]:
    """Run a route up to a bound background implementation stage."""
    from grokbuild.state import load_state

    prompt = "route=implement: authentication code"
    plant_session(tmp / "grok", prompt)
    run_main({"hookEventName": "UserPromptSubmit", "sessionId": SID, "prompt": prompt})
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": "explore"},
            "toolResult": "repository map complete",
        }
    )
    state = load_state(default_state_path())
    decision_id, track = next(iter(state.executions.items()))
    role = track.next_required_role()
    run_main(
        {
            "hookEventName": "PreToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": role, "background": True},
        }
    )
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "spawn_subagent",
            "toolInput": {"subagent_type": role, "background": True},
            "toolResult": (PAYLOAD_FIXTURES / "background-ack-multiline.txt").read_text(),
        }
    )
    return decision_id, role


def _terminal_payload(body: str) -> str:
    return (
        f"=== Task {TASK_ID} ===\n"
        "Command: [subagent:implement] ...\n"
        "Status: completed\n"
        "Exit Code: 0\n\n"
        "=== Output ===\n"
        f"{body}\n"
    )


def test_spec_is_attached_before_the_child_spawns(tmp: Path) -> None:
    """The contract must exist by the time the spawn is allowed, not after."""
    setup_environment(tmp)
    decision_id, role = _drive_linear_stage(tmp)
    spec = find_task_spec(decision_id, role)
    check(spec is not None, f"a spec is attached for the allowed stage ({decision_id}/{role})")
    check(
        spec is not None and spec.role == role and spec.session_id == SID,
        "the spec names the stage it governs",
    )
    check(
        find_task_spec(decision_id, "explore") is not None,
        "the earlier recon stage also carries a spec",
    )


def test_legacy_route_still_completes_on_prose(tmp: Path) -> None:
    """The default profile must behave exactly as it did before R2."""
    from grokbuild.state import load_state

    setup_environment(tmp)
    decision_id, role = _drive_linear_stage(tmp)
    run_main(
        {
            "hookEventName": "PostToolUse",
            "sessionId": SID,
            "toolName": "get_command_or_subagent_output",
            "toolInput": {"task_id": TASK_ID},
            "toolResult": _terminal_payload("implemented the change"),
        }
    )
    track = load_state(default_state_path()).get_execution(decision_id)
    check(role in track.completed, f"prose still completes under the legacy policy ({role})")
    records = [
        json.loads(line) for line in task_evidence_path().read_text(encoding="utf-8").splitlines()
    ]
    outcomes = [item for item in records if item.get("schema") == TASK_EVIDENCE_SCHEMA]
    check(bool(outcomes), "an evidence outcome is recorded even under the legacy policy")
    check(
        outcomes[-1]["typed_result"] is False and outcomes[-1]["reason_codes"] == ["not_evaluated"],
        f"the record shows an untyped result and no claimed verdict ({outcomes[-1]})",
    )


def _last_evidence_outcome() -> dict:
    records = [
        json.loads(line) for line in task_evidence_path().read_text(encoding="utf-8").splitlines()
    ]
    return [item for item in records if item.get("schema") == TASK_EVIDENCE_SCHEMA][-1]


def _typed_result(decision_id: str, stage_key: str) -> dict:
    return {
        "schema": "r2-task-result-v1",
        "contract_version": 1,
        "decision_id": decision_id,
        "stage_key": stage_key,
        "task_id": TASK_ID,
        "status": "success",
        "changed_paths": [],
        "artifacts": [],
        "checks": [],
        "criteria": [],
        "unresolved": [],
        "observed_at": "2026-08-31T00:00:00Z",
    }


def test_strict_route_refuses_prose(tmp: Path) -> None:
    """A terminal prose result carries no evidence, so it cannot complete a stage."""
    from grokbuild.state import load_state

    setup_environment(tmp)
    os.environ["GROK_ROUTE_PROFILE"] = "evidence"
    try:
        decision_id, role = _drive_linear_stage(tmp)
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_id": TASK_ID},
                "toolResult": _terminal_payload("implemented the change"),
            }
        )
        track = load_state(default_state_path()).get_execution(decision_id)
        check(
            role not in track.completed,
            f"strict policy refuses to complete a stage on prose ({track.completed})",
        )
        spec = find_task_spec(decision_id, role)
        check(
            spec is not None and spec.checks == (),
            f"the stage contract is identity only ({spec})",
        )
        outcome = _last_evidence_outcome()
        check(
            outcome["policy"] == "strict"
            and outcome["typed_result"] is False
            and outcome["reason_codes"] == ["result_missing"],
            f"the refusal is recorded with its reason ({outcome})",
        )
    finally:
        os.environ.pop("GROK_ROUTE_PROFILE", None)


def test_strict_route_rejects_an_empty_typed_result(tmp: Path) -> None:
    from grokbuild.state import load_state

    setup_environment(tmp)
    os.environ["GROK_ROUTE_PROFILE"] = "evidence"
    try:
        decision_id, role = _drive_linear_stage(tmp)
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_id": TASK_ID},
                "toolResult": _terminal_payload(
                    "done\n```json\n" + json.dumps(_typed_result(decision_id, role)) + "\n```"
                ),
            }
        )
        track = load_state(default_state_path()).get_execution(decision_id)
        check(
            role not in track.completed,
            f"strict policy rejects an empty typed result ({track.completed})",
        )
        outcome = _last_evidence_outcome()
        check(
            outcome["verified"] is False
            and outcome["typed_result"] is True
            and outcome["reason_codes"] == ["result_malformed"],
            f"the empty result is recorded as malformed ({outcome})",
        )
    finally:
        os.environ.pop("GROK_ROUTE_PROFILE", None)


def test_strict_route_refuses_a_result_for_another_stage(tmp: Path) -> None:
    """A typed result must be for the stage whose contract is in force."""
    from grokbuild.state import load_state

    setup_environment(tmp)
    os.environ["GROK_ROUTE_PROFILE"] = "evidence"
    try:
        decision_id, role = _drive_linear_stage(tmp)
        typed = _typed_result(decision_id, "some-other-stage")
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_id": TASK_ID},
                "toolResult": _terminal_payload("done\n```json\n" + json.dumps(typed) + "\n```"),
            }
        )
        track = load_state(default_state_path()).get_execution(decision_id)
        check(
            role not in track.completed,
            f"a result for another stage cannot complete this one ({track.completed})",
        )
        outcome = _last_evidence_outcome()
        check(
            "stage_mismatch" in outcome["reason_codes"],
            f"the mismatch is named ({outcome['reason_codes']})",
        )
    finally:
        os.environ.pop("GROK_ROUTE_PROFILE", None)


def test_strict_route_refuses_an_unverifiable_claim(tmp: Path) -> None:
    """A change the runtime cannot see in the worktree is not evidence."""
    from grokbuild.state import load_state

    setup_environment(tmp)
    os.environ["GROK_ROUTE_PROFILE"] = "evidence"
    try:
        decision_id, role = _drive_linear_stage(tmp)
        typed = _typed_result(decision_id, role)
        typed["changed_paths"] = ["src/never-touched-by-this-run.py"]
        run_main(
            {
                "hookEventName": "PostToolUse",
                "sessionId": SID,
                "toolName": "get_command_or_subagent_output",
                "toolInput": {"task_id": TASK_ID},
                "toolResult": _terminal_payload("done\n```json\n" + json.dumps(typed) + "\n```"),
            }
        )
        track = load_state(default_state_path()).get_execution(decision_id)
        check(
            role not in track.completed,
            f"an unconfirmed change claim cannot complete a stage ({track.completed})",
        )
        outcome = _last_evidence_outcome()
        check(
            "diff_missing_declared" in outcome["reason_codes"],
            f"the unconfirmed claim is named ({outcome['reason_codes']})",
        )
    finally:
        os.environ.pop("GROK_ROUTE_PROFILE", None)
