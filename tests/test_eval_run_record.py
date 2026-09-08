#!/usr/bin/env python3
from __future__ import annotations

from _harness import run_standalone
import json
from pathlib import Path

from _harness import bootstrap

bootstrap()
from eval.run_record import METRICS, REQUIRED, digest, is_valid_run_record, validate_run_record
import jsonschema


def valid():
    controls = {
        "fixture": {
            "id": "t",
            "version": 1,
            "rubric": {},
            "acceptance": [{"kind": "pytest", "spec": "test_calc.py::test_add"}],
            "rubric_digest": digest({}),
            "acceptance_digest": digest([{"kind": "pytest", "spec": "test_calc.py::test_add"}]),
            "freeze": "now",
        },
        "variant": {"namespace": "full-pipeline", "definition_version": 1, "invariant_digest": "z"},
        "environment": {
            "repo_commit": "abc",
            "installer_revision": "i",
            "runtime": "python",
            "host": "test",
            "sandbox": "isolated",
        },
        "permissions": {
            "tools": [],
            "verifier_command": "controller",
            "network": "off",
            "writable_path": [],
        },
        "initial_state": {"digest": "d"},
        "ordering": {
            "trial_index": 0,
            "policy": "fixed",
            "order": "trial",
            "seed": 1,
            "repetition_count": 1,
        },
        "pairing": {"id": "p1", "rule": "same controls"},
        "verifier": {"command": "controller", "network": "off", "writable_path": []},
    }
    record = {
        "schema_version": 2,
        "run_id": "r1",
        "candidate_supply": 1.0,
        "failure_kind": None,
        "candidate_portfolio": ["A", "B"],
        "downstream_verifier_stack": ["pytest", "hidden-tests"],
        "judge_versions": ["artifact-judge-v1"],
        "selection_policy": {
            "name": "selector-v2",
            "bindings": ["gpt-5.6-luna@high", "glm-5.3-flash@default"],
        },
        **controls,
        "roles": [
            {
                "role": "implement-standard",
                "provider": "p",
                "model": "m",
                "effort": "max",
                "available": True,
            }
        ],
        "start": {},
        "termination": {},
        "outcome": "success",
        "invalid": {"boolean": False, "class": "none", "evidence": "no setup failure"},
        "telemetry": {},
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "source": "provider",
            "missing_data": "none",
        },
        "tool_calls": 0,
        "retries": 0,
        "stop_blocks": 0,
        "changed_paths": [],
        "adjudication": {},
    }
    record["metrics"] = {
        name: {"numerator": 0, "denominator": 1, "source": "controller", "missing_data": "none"}
        for name in METRICS
    }
    return record


def valid_v3():
    record = valid()
    record["schema_version"] = 3
    record["judge_health_snapshot"] = [{
        "health_id": "h1", "provider": "p", "endpoint": "e",
        "requested_model": "m", "resolved_model": "snapshot", "prompt_hash": "hash",
        "rubric_version": "v1", "temperature": 0.0, "reasoning_effort": "high",
        "response_schema": "judge-opinion-v2", "canary_battery_version": "v1",
        "repeatability": 0.9, "position_bias": 0.1,
    }]
    record["judge_health_snapshot_digest"] = digest(record["judge_health_snapshot"])
    record["comparison_aggregates"] = [{
        "comparison_id": "c1", "candidate_a": "A", "candidate_b": "B",
        "base_reads": [
            {"first": "A", "second": "B", "prefers": "A"},
            {"first": "B", "second": "A", "prefers": "A"},
        ],
        "additional_reads": [], "final": "A", "margin": 1.0,
        "deterministic_status": "proceed", "rejected_candidates": [],
    }]
    record["canary_battery_digest"] = "canary"
    record["holdout_manifest_digest"] = "holdout"
    record["capability_boundary_snapshot"] = {
        "proposer": ["propose"], "applier": ["apply"], "judge": ["judge"],
        "grader": ["grade"], "network": "off", "hermetic": True,
    }
    record["capability_boundary_digest"] = digest(record["capability_boundary_snapshot"])
    return record


def test_valid_record_passes():
    assert is_valid_run_record(valid())


def test_v3_judge_snapshots_validate_digests_aggregate_and_boundaries():
    record = valid()
    record["schema_version"] = 3
    record["judge_health_snapshot"] = [{
        "health_id": "h1", "provider": "p", "endpoint": "e",
        "requested_model": "m", "resolved_model": "snapshot", "prompt_hash": "hash",
        "rubric_version": "v1", "temperature": 0.0, "reasoning_effort": "high",
        "response_schema": "judge-opinion-v2", "canary_battery_version": "v1",
        "repeatability": 0.9, "position_bias": 0.1,
    }]
    record["judge_health_snapshot_digest"] = digest(record["judge_health_snapshot"])
    record["comparison_aggregates"] = [{
        "comparison_id": "c1", "candidate_a": "A", "candidate_b": "B",
        "base_reads": [
            {"first": "A", "second": "B", "prefers": "A"},
            {"first": "B", "second": "A", "prefers": "A"},
        ],
        "additional_reads": [], "final": "A", "margin": 1.0,
        "deterministic_status": "proceed", "rejected_candidates": [],
    }]
    record["canary_battery_digest"] = "canary"
    record["holdout_manifest_digest"] = "holdout"
    record["capability_boundary_snapshot"] = {
        "proposer": ["propose"], "applier": ["apply"], "judge": ["judge"],
        "grader": ["grade"], "network": "off", "hermetic": True,
    }
    record["capability_boundary_digest"] = digest(record["capability_boundary_snapshot"])
    assert is_valid_run_record(record)
    record["comparison_aggregates"][0]["rejected_candidates"] = ["A"]
    assert any("reject dominance" in error for error in validate_run_record(record))


def test_v3_winner_must_follow_reads_and_proceeding_gate_evidence():
    contradictory = valid_v3()
    contradictory["comparison_aggregates"][0]["final"] = "B"
    assert any("unsupported by its reads" in error for error in validate_run_record(contradictory))
    missing_gate = valid_v3()
    del missing_gate["comparison_aggregates"][0]["deterministic_status"]
    assert any("lacks proceeding gate evidence" in error for error in validate_run_record(missing_gate))
    abstained = valid_v3()
    abstained["comparison_aggregates"][0]["base_reads"][0]["prefers"] = "abstain"
    assert any("unsupported by its reads" in error for error in validate_run_record(abstained))


def test_v3_validates_candidate_identities_and_every_reread():
    record = valid_v3()
    aggregate = record["comparison_aggregates"][0]
    aggregate["additional_reads"] = [
        {"first": "A", "second": "C", "prefers": "A", "invocation_id": "same"},
        {"first": "A", "second": "B", "prefers": "invalid", "invocation_id": "same"},
    ]
    errors = validate_run_record(record)
    assert any("invalid pair order" in error for error in errors)
    assert any("invalid preference" in error for error in errors)
    assert any("repeats an invocation_id" in error for error in errors)
    aggregate["candidate_a"] = "outside-portfolio"
    assert any("invalid candidate identities" in error for error in validate_run_record(record))


def test_v3_read_confidence_must_be_within_probability_range():
    for confidence in (-0.01, 1.01):
        record = valid_v3()
        record["comparison_aggregates"][0]["base_reads"][0]["confidence"] = confidence
        assert any("invalid confidence" in error for error in validate_run_record(record))
        try:
            jsonschema.validate(record, schema())
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError(f"schema accepted out-of-range confidence: {confidence}")


def test_v3_malformed_aggregate_containers_return_errors_instead_of_crashing():
    for field, malformed in (("base_reads", {}), ("additional_reads", "bad"), ("rejected_candidates", {})):
        record = valid_v3()
        record["comparison_aggregates"][0][field] = malformed
        assert validate_run_record(record)


def test_v3_grader_capabilities_must_be_explicit_and_disjoint():
    missing = valid_v3()
    del missing["capability_boundary_snapshot"]["grader"]
    missing["capability_boundary_digest"] = digest(missing["capability_boundary_snapshot"])
    assert any("grader must be a string list" in error for error in validate_run_record(missing))
    overlap = valid_v3()
    overlap["capability_boundary_snapshot"]["grader"] = ["judge"]
    overlap["capability_boundary_digest"] = digest(overlap["capability_boundary_snapshot"])
    assert any("judge/grader" in error for error in validate_run_record(overlap))


def test_forbidden_material_key_normalization_catches_camel_case() -> None:
    record = valid()
    record["schema_version"] = 3
    record["judge_health_snapshot"] = [{
        "health_id": "h1", "provider": "p", "endpoint": "e",
        "requested_model": "m", "resolved_model": "m", "prompt_hash": "h",
        "rubric_version": "v1", "temperature": 0.0, "reasoning_effort": "high",
        "response_schema": "judge-opinion-v2", "canary_battery_version": "v1",
        "repeatability": 1.0, "position_bias": 0.0,
    }]
    record["judge_health_snapshot_digest"] = digest(record["judge_health_snapshot"])
    record["comparison_aggregates"] = [{
        "comparison_id": "c1", "candidate_a": "A", "candidate_b": "B",
        "base_reads": [
            {"first": "A", "second": "B", "prefers": "tie"},
            {"first": "B", "second": "A", "prefers": "tie"},
        ], "additional_reads": [], "final": "tie", "margin": 0.0,
        "deterministic_status": "proceed", "rejected_candidates": [],
        "referenceSolution": "must be rejected",
    }]
    record["canary_battery_digest"] = "canary"
    record["holdout_manifest_digest"] = "holdout"
    record["capability_boundary_snapshot"] = {
        "proposer": ["propose"], "applier": ["apply"], "judge": ["judge"],
        "network": "off", "hermetic": True,
    }
    record["capability_boundary_digest"] = digest(record["capability_boundary_snapshot"])
    assert any("forbidden reference material" in error for error in validate_run_record(record))


def schema():
    return json.loads((Path(__file__).parents[1] / "eval/run-record.schema.json").read_text())


def test_schema_lists_every_required_control_and_metric():
    loaded = schema()
    assert set(REQUIRED) <= set(loaded["required"])
    assert set(METRICS) <= set(loaded["properties"]["metrics"]["required"])
    assert set(REQUIRED) <= set(loaded["properties"])


def test_complete_record_passes_json_schema():
    jsonschema.validate(valid(), schema())


def test_same_wrong_type_cases_rejected_by_schema_and_public_path():
    for section, field, wrong in (
        ("ordering", "trial_index", "zero"),
        ("permissions", "tools", [1, 2]),
        ("permissions", "network", [1]),
    ):
        record = valid()
        record[section][field] = wrong
        try:
            jsonschema.validate(record, schema())
        except jsonschema.ValidationError:
            pass
        else:
            raise AssertionError(f"schema accepted wrong type: {section}.{field}")
        assert validate_run_record(record)


def test_fixture_version_wrong_type_rejected_by_both_paths():
    record = valid()
    record["fixture"]["version"] = []
    try:
        jsonschema.validate(record, schema())
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("schema accepted wrong fixture version type")
    assert "fixture.version has invalid type" in validate_run_record(record)


def test_acceptance_uses_mappings_not_string_list():
    record = valid()
    record["fixture"]["acceptance"] = [{"kind": "pytest", "spec": "test_calc.py::test_add"}]
    assert is_valid_run_record(record)
    record["fixture"]["acceptance"] = ["test_calc.py::test_add"]
    assert "fixture.acceptance has invalid type" in validate_run_record(record)


def test_each_required_control_deletion_fails():
    for section, fields in {
        "fixture": ["id", "version", "rubric_digest", "acceptance_digest", "freeze"],
        "variant": ["namespace", "definition_version", "invariant_digest"],
        "environment": ["repo_commit", "installer_revision", "runtime", "host", "sandbox"],
        "permissions": ["tools", "verifier_command", "network", "writable_path"],
        "initial_state": ["digest"],
        "ordering": ["trial_index", "policy", "order", "seed", "repetition_count"],
        "pairing": ["id", "rule"],
        "verifier": ["command", "network", "writable_path"],
    }.items():
        for field in fields:
            record = valid()
            del record[section][field]
            assert validate_run_record(record)


def test_each_required_control_type_fails():
    sections = {
        "fixture": ["id", "version", "rubric_digest", "acceptance_digest", "freeze"],
        "variant": ["namespace", "definition_version", "invariant_digest"],
        "environment": ["repo_commit", "installer_revision", "runtime", "host", "sandbox"],
        "permissions": ["tools", "verifier_command", "network", "writable_path"],
        "initial_state": ["digest"],
        "ordering": ["trial_index", "policy", "order", "seed", "repetition_count"],
        "pairing": ["id", "rule"],
        "verifier": ["command", "network", "writable_path"],
    }
    for section, fields in sections.items():
        for field in fields:
            record = valid()
            record[section][field] = None
            assert f"{section}.{field} has invalid type" in validate_run_record(record)


def test_each_metric_deletion_type_and_missing_data_fails():
    for name in METRICS:
        record = valid()
        del record["metrics"][name]
        assert f"metric missing: {name}" in validate_run_record(record)
        record = valid()
        record["metrics"][name] = []
        assert f"metric missing: {name}" in validate_run_record(record)
        record = valid()
        del record["metrics"][name]["missing_data"]
        assert f"metric {name} missing missing_data" in validate_run_record(record)


def test_outcome_combinations_are_enforced_by_both_validators():
    for outcome, invalid_flag in (("success", False), ("failure", False), ("invalid", True)):
        record = valid()
        record["outcome"] = outcome
        record["failure_kind"] = "selection" if outcome == "failure" else None
        record["invalid"] = {
            "boolean": invalid_flag,
            "class": "setup" if invalid_flag else "none",
            "evidence": "evidence",
        }
        assert is_valid_run_record(record)
        jsonschema.validate(record, schema())
    record = valid()
    record["outcome"] = "invalid"
    record["invalid"]["boolean"] = False
    assert validate_run_record(record)
    try:
        jsonschema.validate(record, schema())
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("schema accepted invalid outcome combination")

    record = valid()
    record["outcome"] = "success"
    record["invalid"]["boolean"] = True
    assert validate_run_record(record)
    try:
        jsonschema.validate(record, schema())
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("schema accepted scored invalid combination")


def test_failure_attribution_matches_outcome_and_candidate_supply():
    generation = valid()
    generation["outcome"] = "failure"
    generation["candidate_supply"] = 0.0
    generation["failure_kind"] = "generation"
    assert is_valid_run_record(generation)
    jsonschema.validate(generation, schema())

    selection = valid()
    selection["outcome"] = "failure"
    selection["candidate_supply"] = 1.0
    selection["failure_kind"] = "selection"
    assert is_valid_run_record(selection)
    jsonschema.validate(selection, schema())

    success = valid()
    success["failure_kind"] = "selection"
    assert validate_run_record(success)
    try:
        jsonschema.validate(success, schema())
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("schema accepted failure attribution on a successful run")


def test_digest_mismatch_is_rejected():
    record = valid()
    record["fixture"]["rubric_digest"] = "wrong"
    jsonschema.validate(record, schema())
    assert "fixture rubric_digest mismatch" in validate_run_record(record)
    record["fixture"]["acceptance_digest"] = "wrong"
    jsonschema.validate(record, schema())
    assert "fixture acceptance_digest mismatch" in validate_run_record(record)


def test_invalid_trial_requires_class_and_evidence():
    record = valid()
    record["invalid"] = {"boolean": True}
    assert "invalid class and evidence are required" in validate_run_record(record)


def test_unknown_variant_fails():
    record = valid()
    record["variant"]["namespace"] = "unknown"
    assert "variant namespace is unsupported" in validate_run_record(record)


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
    "test_forbidden_material_key_normalization_catches_camel_case",
    "test_valid_record_passes",
    "test_v3_judge_snapshots_validate_digests_aggregate_and_boundaries",
    "test_v3_winner_must_follow_reads_and_proceeding_gate_evidence",
    "test_v3_validates_candidate_identities_and_every_reread",
    "test_v3_malformed_aggregate_containers_return_errors_instead_of_crashing",
    "test_v3_read_confidence_must_be_within_probability_range",
    "test_v3_grader_capabilities_must_be_explicit_and_disjoint",
    "test_schema_lists_every_required_control_and_metric",
    "test_complete_record_passes_json_schema",
    "test_same_wrong_type_cases_rejected_by_schema_and_public_path",
    "test_fixture_version_wrong_type_rejected_by_both_paths",
    "test_acceptance_uses_mappings_not_string_list",
    "test_each_required_control_deletion_fails",
    "test_each_required_control_type_fails",
    "test_each_metric_deletion_type_and_missing_data_fails",
    "test_outcome_combinations_are_enforced_by_both_validators",
    "test_failure_attribution_matches_outcome_and_candidate_supply",
    "test_digest_mismatch_is_rejected",
    "test_invalid_trial_requires_class_and_evidence",
    "test_unknown_variant_fails",
)
