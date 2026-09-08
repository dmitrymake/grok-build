"""Versioned offline R1 run-record contract and structural validator."""

from __future__ import annotations
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = frozenset({2, 3})
V3_REQUIRED = (
    "judge_health_snapshot",
    "judge_health_snapshot_digest",
    "comparison_aggregates",
    "canary_battery_digest",
    "holdout_manifest_digest",
    "capability_boundary_snapshot",
    "capability_boundary_digest",
)
FORBIDDEN_JUDGE_MATERIAL = frozenset(
    {"reference_solution", "expected_patch", "answer_key", "verifier_source", "trajectory", "chain_of_thought"}
)
VARIANTS = {
    "single-best-agent",
    "full-pipeline",
    "no-recon",
    "no-LLM-review",
    "verifier-only",
    "same-provider-reviewer",
    "cross-provider-reviewer",
    "fixed-topology",
    "adaptive-topology",
}
METRICS = (
    "task_success",
    "hidden_test_success",
    "false_success",
    "false_blocked",
    "unnecessary_agent_rate",
    "tokens",
    "wall_clock",
    "actual_tool_calls",
    "retries",
    "stop_blocks",
    "files_changed_outside_scope",
)
METRIC_FIELDS = ("numerator", "denominator", "source", "missing_data")
CONTROL_FIELDS = {
    "fixture": (
        "id",
        "version",
        "rubric",
        "acceptance",
        "rubric_digest",
        "acceptance_digest",
        "freeze",
    ),
    "variant": ("namespace", "definition_version", "invariant_digest"),
    "environment": ("repo_commit", "installer_revision", "runtime", "host", "sandbox"),
    "permissions": ("tools", "verifier_command", "network", "writable_path"),
    "initial_state": ("digest",),
    "ordering": ("trial_index", "policy", "order", "seed", "repetition_count"),
    "pairing": ("id", "rule"),
    "verifier": ("command", "network", "writable_path"),
}
TYPE_RULES = {
    "fixture": {
        "id": (str,),
        "version": (int, str),
        "rubric": (Mapping,),
        "acceptance": (list,),
        "rubric_digest": (str,),
        "acceptance_digest": (str,),
        "freeze": (str, Mapping),
    },
    "variant": {"namespace": (str,), "definition_version": (int,), "invariant_digest": (str,)},
    "environment": {
        field: (str,)
        for field in ("repo_commit", "installer_revision", "runtime", "host", "sandbox")
    },
    "permissions": {
        "tools": (list, str),
        "verifier_command": (str, list),
        "network": (str, list),
        "writable_path": (str, list),
    },
    "initial_state": {"digest": (str,)},
    "ordering": {
        "trial_index": (int,),
        "policy": (int, str),
        "order": (int, str),
        "seed": (int, str),
        "repetition_count": (int, str),
    },
    "pairing": {"id": (str,), "rule": (str,)},
    "verifier": {"command": (str,), "network": (str, list), "writable_path": (str, list)},
}
REQUIRED = (
    "schema_version",
    "run_id",
    "fixture",
    "variant",
    "environment",
    "roles",
    "permissions",
    "initial_state",
    "ordering",
    "pairing",
    "start",
    "termination",
    "outcome",
    "invalid",
    "telemetry",
    "usage",
    "tool_calls",
    "retries",
    "stop_blocks",
    "changed_paths",
    "verifier",
    "adjudication",
    "candidate_supply",
    "failure_kind",
    "candidate_portfolio",
    "downstream_verifier_stack",
    "judge_versions",
    "selection_policy",
    "metrics",
)


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_cross_fields(record: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return ["record must be an object"]
    errors.extend(f"missing field: {key}" for key in REQUIRED if key not in record)
    version = record.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}")
    if version == 3:
        errors.extend(f"missing field: {key}" for key in V3_REQUIRED if key not in record)
        health = record.get("judge_health_snapshot")
        if not isinstance(health, list) or not health:
            errors.append("judge_health_snapshot must be a non-empty list")
        elif record.get("judge_health_snapshot_digest") != digest(health):
            errors.append("judge_health_snapshot_digest mismatch")
        boundary = record.get("capability_boundary_snapshot")
        if not isinstance(boundary, Mapping):
            errors.append("capability_boundary_snapshot must be an object")
        else:
            if record.get("capability_boundary_digest") != digest(boundary):
                errors.append("capability_boundary_digest mismatch")
            if boundary.get("network") != "off" or boundary.get("hermetic") is not True:
                errors.append("capability boundary must be hermetic with network off")
            actor_sets = []
            for actor in ("proposer", "applier", "judge", "grader"):
                values = boundary.get(actor)
                if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                    errors.append(f"capability boundary {actor} must be a string list")
                else:
                    actor_sets.append((actor, set(values)))
            for index, (left_name, left) in enumerate(actor_sets):
                for right_name, right in actor_sets[index + 1 :]:
                    if left & right:
                        errors.append(f"capability boundaries overlap: {left_name}/{right_name}")
        aggregates = record.get("comparison_aggregates")
        if not isinstance(aggregates, list):
            errors.append("comparison_aggregates must be a list")
        else:
            for index, aggregate in enumerate(aggregates):
                if not isinstance(aggregate, Mapping):
                    errors.append(f"comparison_aggregates[{index}] must be an object")
                    continue
                prefix = f"comparison_aggregates[{index}]"
                a, b = aggregate.get("candidate_a"), aggregate.get("candidate_b")
                portfolio = record.get("candidate_portfolio")
                identities_valid = (
                    isinstance(a, str)
                    and bool(a)
                    and isinstance(b, str)
                    and bool(b)
                    and a != b
                    and isinstance(portfolio, list)
                    and a in portfolio
                    and b in portfolio
                )
                if not identities_valid:
                    errors.append(f"{prefix} has invalid candidate identities")
                base = aggregate.get("base_reads")
                additional = aggregate.get("additional_reads")
                if not isinstance(base, list):
                    errors.append(f"{prefix} base_reads must be a list")
                    base = []
                if not isinstance(additional, list):
                    errors.append(f"{prefix} additional_reads must be a list")
                    additional = []
                reads_valid = identities_valid and len(base) == 2
                valid_pairs = ((a, b), (b, a)) if identities_valid else ()
                orders: set[tuple[str, str]] = set()
                votes: list[str] = []
                invocation_ids: list[str] = []
                for read_index, item in enumerate((*base, *additional)):
                    if not isinstance(item, Mapping):
                        errors.append(f"{prefix} read[{read_index}] must be an object")
                        reads_valid = False
                        continue
                    first, second, preference = (
                        item.get("first"),
                        item.get("second"),
                        item.get("prefers"),
                    )
                    if (first, second) not in valid_pairs:
                        errors.append(f"{prefix} read[{read_index}] has an invalid pair order")
                        reads_valid = False
                    elif read_index < len(base):
                        orders.add((first, second))
                    if preference not in (a, b, "abstain"):
                        errors.append(f"{prefix} read[{read_index}] has an invalid preference")
                        reads_valid = False
                    elif preference != "abstain":
                        votes.append(preference)
                    confidence = item.get("confidence")
                    if confidence is not None and (
                        not _number(confidence) or not 0.0 <= float(confidence) <= 1.0
                    ):
                        errors.append(f"{prefix} read[{read_index}] has invalid confidence")
                        reads_valid = False
                    invocation_id = item.get("invocation_id")
                    if read_index >= len(base) and (
                        not isinstance(invocation_id, str) or not invocation_id
                    ):
                        errors.append(f"{prefix} reread[{read_index - len(base)}] lacks invocation_id")
                        reads_valid = False
                    if isinstance(invocation_id, str) and invocation_id:
                        invocation_ids.append(invocation_id)
                if identities_valid and orders != set(valid_pairs):
                    errors.append(f"{prefix} requires complete AB+BA reads")
                    reads_valid = False
                if len(invocation_ids) != len(set(invocation_ids)):
                    errors.append(f"{prefix} repeats an invocation_id")
                    reads_valid = False
                rejected = aggregate.get("rejected_candidates")
                if not isinstance(rejected, list) or not all(
                    isinstance(item, str) and identities_valid and item in (a, b)
                    for item in rejected
                ):
                    errors.append(f"{prefix} rejected_candidates must contain candidate identities")
                    rejected = []
                final = aggregate.get("final")
                if final in {"A", "B"}:
                    selected = a if final == "A" else b
                    deterministic_status = aggregate.get("deterministic_status")
                    if deterministic_status != "proceed":
                        errors.append(f"{prefix} winning result lacks proceeding gate evidence")
                    if selected in rejected:
                        errors.append(f"{prefix} violates deterministic reject dominance")
                    base_preferences = [
                        item.get("prefers") for item in base if isinstance(item, Mapping)
                    ]
                    count_a, count_b = votes.count(a), votes.count(b)
                    expected_final = "A" if count_a > count_b else "B" if count_b > count_a else "ABSTAIN"
                    expected_margin = abs(count_a - count_b) / len(votes) if votes else 0.0
                    declared_margin = aggregate.get("margin")
                    if (
                        not reads_valid
                        or "abstain" in base_preferences
                        or final != expected_final
                        or not _number(declared_margin)
                        or not math.isclose(float(declared_margin), expected_margin)
                    ):
                        errors.append(f"{prefix} winning result is unsupported by its reads")
                def contains_forbidden(value: Any) -> bool:
                    if isinstance(value, Mapping):
                        return any(
                            re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).casefold().replace("-", "_")
                            in FORBIDDEN_JUDGE_MATERIAL
                            or contains_forbidden(item)
                            for key, item in value.items()
                        )
                    if isinstance(value, list):
                        return any(contains_forbidden(item) for item in value)
                    return False
                if contains_forbidden(aggregate):
                    errors.append(f"comparison_aggregates[{index}] contains forbidden reference material")
    candidate_supply = record.get("candidate_supply")
    if not _number(candidate_supply) or not 0.0 <= float(candidate_supply) <= 1.0:
        errors.append("candidate_supply must be a number from 0 to 1")
    failure_kind = record.get("failure_kind")
    if failure_kind not in {None, "generation", "selection"}:
        errors.append("failure_kind must be null, generation, or selection")
    outcome = record.get("outcome")
    if outcome in {"success", "invalid"} and failure_kind is not None:
        errors.append("successful and invalid outcomes cannot claim failure attribution")
    elif outcome == "failure" and _number(candidate_supply):
        expected_kind = "generation" if float(candidate_supply) == 0.0 else "selection"
        if failure_kind != expected_kind:
            errors.append(f"failed outcome with this candidate_supply requires {expected_kind}")
    for field in ("candidate_portfolio", "downstream_verifier_stack", "judge_versions"):
        value = record.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{field} must be a string list")
    selection_policy = record.get("selection_policy")
    if (
        not isinstance(selection_policy, Mapping)
        or not isinstance(selection_policy.get("name"), str)
        or not selection_policy.get("name")
        or not isinstance(selection_policy.get("bindings"), list)
        or not all(isinstance(item, str) and "@" in item for item in selection_policy["bindings"])
    ):
        errors.append("selection_policy must name the policy and model@effort bindings")
    for section, fields in CONTROL_FIELDS.items():
        value = record.get(section)
        if not isinstance(value, Mapping):
            errors.append(f"{section} must be an object")
            continue
        for field in fields:
            if field not in value:
                errors.append(f"{section} missing {field}")
            elif (
                not isinstance(value[field], TYPE_RULES[section][field])
                or (
                    section == "ordering"
                    and field in {"trial_index", "policy", "order", "seed", "repetition_count"}
                    and isinstance(value[field], bool)
                )
            ) or (
                section == "permissions"
                and field in {"tools", "network", "verifier_command", "writable_path"}
                and isinstance(value[field], list)
                and not all(isinstance(item, str) for item in value[field])
            ):
                errors.append(f"{section}.{field} has invalid type")
    fixture = record.get("fixture", {})
    if isinstance(fixture, Mapping):
        if (
            "rubric_digest" in fixture
            and "rubric" in fixture
            and fixture["rubric_digest"] != digest(fixture["rubric"])
        ):
            errors.append("fixture rubric_digest mismatch")
        if (
            "acceptance_digest" in fixture
            and "acceptance" in fixture
            and fixture["acceptance_digest"] != digest(fixture["acceptance"])
        ):
            errors.append("fixture acceptance_digest mismatch")
        if "version" in fixture and not isinstance(fixture["version"], (int, str)):
            errors.append("fixture.version has invalid type")
        acceptance = fixture.get("acceptance")
        if not isinstance(acceptance, list) or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("kind"), str)
            or not item["kind"]
            or not isinstance(item.get("spec"), str)
            or not item["spec"]
            for item in acceptance
        ):
            errors.append("fixture.acceptance has invalid type")
    variant = record.get("variant", {})
    if isinstance(variant, Mapping) and variant.get("namespace") not in VARIANTS:
        errors.append("variant namespace is unsupported")
    roles = record.get("roles")
    if not isinstance(roles, list) or not roles:
        errors.append("roles must be a non-empty list")
    else:
        for index, role in enumerate(roles):
            if not isinstance(role, Mapping):
                errors.append(f"roles[{index}] must be an object")
            elif not all(
                isinstance(role.get(key), str) and role[key]
                for key in ("role", "provider", "model", "effort")
            ) or not isinstance(role.get("available"), bool):
                errors.append(f"roles[{index}] has invalid binding types")
    invalid = record.get("invalid")
    if not isinstance(invalid, Mapping) or not isinstance(invalid.get("boolean"), bool):
        errors.append("invalid.boolean must be boolean")
    elif (
        not isinstance(invalid.get("class"), str)
        or not invalid["class"]
        or not isinstance(invalid.get("evidence"), str)
        or not invalid["evidence"]
    ):
        errors.append("invalid class and evidence are required")
    outcome = record.get("outcome")
    if outcome not in {"success", "failure", "invalid"}:
        errors.append("outcome must be success, failure, or invalid")
    elif isinstance(invalid, Mapping):
        if outcome == "invalid" and invalid.get("boolean") is not True:
            errors.append("invalid outcome requires invalid.boolean true")
        if outcome in {"success", "failure"} and invalid.get("boolean") is not False:
            errors.append("scored outcome requires invalid.boolean false")
    for field, expected in (
        ("start", Mapping),
        ("termination", Mapping),
        ("telemetry", Mapping),
        ("adjudication", Mapping),
        ("changed_paths", list),
    ):
        if field in record and not isinstance(record[field], expected):
            errors.append(f"{field} has invalid type")
    usage = record.get("usage")
    if not isinstance(usage, Mapping):
        errors.append("usage must be an object")
    else:
        if (
            not _number(usage.get("input_tokens"))
            or not _number(usage.get("output_tokens"))
            or not isinstance(usage.get("source"), str)
            or not usage["source"]
            or not isinstance(usage.get("missing_data"), str)
            or not usage["missing_data"]
        ):
            errors.append("usage has invalid types")
    for field in ("tool_calls", "retries", "stop_blocks"):
        if field in record and (
            not isinstance(record[field], int)
            or isinstance(record[field], bool)
            or record[field] < 0
        ):
            errors.append(f"{field} has invalid type")
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping):
        errors.append("metrics must be an object")
    else:
        for name in METRICS:
            value = metrics.get(name)
            if not isinstance(value, Mapping):
                errors.append(f"metric missing: {name}")
                continue
            if any(field not in value for field in METRIC_FIELDS):
                errors.extend(
                    f"metric {name} missing {field}"
                    for field in METRIC_FIELDS
                    if field not in value
                )
                continue
            if (
                (value["numerator"] is not None and not _number(value["numerator"]))
                or (value["denominator"] is not None and not _number(value["denominator"]))
                or not isinstance(value["source"], str)
                or not value["source"]
                or not isinstance(value["missing_data"], str)
                or not value["missing_data"]
            ):
                errors.append(f"metric {name} has invalid types")
            if value["numerator"] is None and value["missing_data"] == "none":
                errors.append(f"metric {name} null numerator needs missing-data marker")
            if outcome in {"success", "failure"} and (
                value["numerator"] is None
                or value["denominator"] is None
                or value["missing_data"] != "none"
            ):
                errors.append(f"metric {name} is invalid for scored outcome")
    return errors


def validate_run_record(record: Any) -> list[str]:
    """Canonical validation: JSON Schema structure, then Python cross-field checks."""
    errors: list[str] = []
    try:
        import jsonschema

        schema = json.loads(
            (Path(__file__).with_name("run-record.schema.json")).read_text(encoding="utf-8")
        )
        errors.extend(
            error.message for error in jsonschema.Draft202012Validator(schema).iter_errors(record)
        )
    except (ImportError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"schema validation unavailable: {exc}")
    errors.extend(_validate_cross_fields(record))
    return errors


def is_valid_run_record(record: Any) -> bool:
    return not validate_run_record(record)
