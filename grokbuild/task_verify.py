"""Independent verification of a typed task result against its task spec.

This module is deliberately a pure function library: it never spawns, routes,
runs tools, reads the filesystem, touches state, or loops over a trajectory.
Everything it needs about the world arrives as an ``Observation`` built by the
caller. That keeps the acceptance rule testable offline and keeps the runtime,
not the verifier, in charge of obtaining evidence.

The rule it enforces is R2's: a terminal result no longer completes a stage by
itself. Only a typed result whose claims the runtime could independently
confirm counts as evidence of completion. A missing, malformed or unconfirmed
claim is an evidence failure, never a success.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from grokbuild.deterministic_gate import GateCheck

# Reason codes are a closed vocabulary so callers can branch on them and
# datasets can aggregate them without parsing prose.
REASON_CODES = (
    "verified",
    "not_evaluated",
    "result_missing",
    "result_malformed",
    "contract_mismatch",
    "stage_mismatch",
    "status_not_success",
    "artifact_missing",
    "path_outside_scope",
    "observed_path_outside_scope",
    "diff_missing_declared",
    "diff_undeclared_change",
    "check_not_run",
    "check_failed",
    "criteria_unverifiable",
    "criteria_unmet",
    "unresolved_items",
)


@dataclass(frozen=True)
class Observation:
    """What the runtime independently saw, as opposed to what the child claimed.

    ``existing_paths`` and ``changed_paths`` are repository-relative. ``checks``
    maps a deterministic check name to the outcome the runtime itself recorded;
    a check the runtime never ran is simply absent, which is not the same as a
    check that failed.
    """

    existing_paths: frozenset[str] = frozenset()
    changed_paths: tuple[str, ...] = ()
    checks: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Verification:
    """The verdict, with the codes that produced it."""

    verified: bool
    reason_codes: tuple[str, ...]
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "verified": self.verified,
            "reason_codes": list(self.reason_codes),
            "failures": list(self.failures),
        }


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


def _canonical_repo_path(path: str) -> str | None:
    """Return a safe repository-relative POSIX path, or ``None``."""
    if not path or path.startswith("/") or _DRIVE_PATH_RE.match(path):
        return None
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _within_scope(path: str, scope: Sequence[str]) -> bool:
    canonical_path = _canonical_repo_path(path)
    if canonical_path is None:
        return False
    if not scope:
        return True
    for prefix in scope:
        canonical_prefix = _canonical_repo_path(prefix)
        if canonical_prefix is None:
            continue
        if canonical_path == canonical_prefix:
            return True
        if canonical_path.startswith(canonical_prefix + "/"):
            return True
    return False


def _verify_criterion(
    criterion: Mapping[str, object], observation: Observation
) -> tuple[str, str] | None:
    """Return ``(reason_code, detail)`` when a criterion is not independently met."""
    kind = str(criterion.get("kind") or "")
    expected = str(criterion.get("expected") or "")
    if kind in {"path_exists", "path_changed"}:
        canonical_expected = _canonical_repo_path(expected)
        if canonical_expected is None:
            return "criteria_unverifiable", f"unsafe path expectation: {expected}"
        if kind == "path_exists":
            if canonical_expected not in observation.existing_paths:
                return "criteria_unmet", f"path_exists: {canonical_expected}"
            return None
        if canonical_expected not in observation.changed_paths:
            return "criteria_unmet", f"path_changed: {canonical_expected}"
        return None
    if kind == "check_passed":
        outcome = observation.checks.get(expected)
        if outcome is None:
            return "criteria_unverifiable", f"check_passed: {expected} never ran"
        if not outcome:
            return "criteria_unmet", f"check_passed: {expected}"
        return None
    # An unknown criterion kind cannot be confirmed by the runtime, and the
    # result's own word is not evidence, so it fails closed.
    return "criteria_unverifiable", f"unsupported criterion kind: {kind or '<empty>'}"


def verify(spec, result, observation: Observation) -> Verification:
    """Verify one ``TaskResult`` against its ``TaskSpec`` and the observation.

    ``spec`` and ``result`` are duck-typed to keep this module free of imports
    from the persistence layer; they only need the documented attributes.
    """
    codes: list[str] = []
    failures: list[str] = []

    def fail(code: str, detail: str) -> None:
        if code not in codes:
            codes.append(code)
        failures.append(detail)

    if result is None:
        return Verification(False, ("result_missing",), ("no typed result was supplied",))
    if getattr(result, "contract_version", None) != getattr(spec, "contract_version", None):
        fail(
            "contract_mismatch",
            f"result contract {getattr(result, 'contract_version', None)!r} "
            f"!= spec contract {getattr(spec, 'contract_version', None)!r}",
        )
    if result.stage_key != spec.stage_key or result.decision_id != spec.decision_id:
        fail(
            "stage_mismatch",
            f"result is for {result.decision_id}/{result.stage_key}, "
            f"spec is for {spec.decision_id}/{spec.stage_key}",
        )
    if result.status != "success":
        fail("status_not_success", f"declared status: {result.status or '<empty>'}")
    if result.unresolved:
        fail("unresolved_items", "; ".join(result.unresolved))
    # A typed success must carry at least one independently checkable claim.
    # Identity fields alone are not evidence; control-plane/observe-only stages
    # stay on their non-strict completion path and do not call this verifier.
    # A result artifact counts only when the runtime saw it change - a file
    # that merely exists proves nothing about this task. A result criterion
    # counts only when it restates a contract criterion, which the contract
    # loop below verifies; a criterion the child invented cannot be confirmed.
    observed_changes = {
        canonical
        for path in observation.changed_paths
        if (canonical := _canonical_repo_path(path)) is not None
    }
    produced_artifacts = tuple(
        artifact
        for artifact in result.artifacts
        if _canonical_repo_path(artifact) in observed_changes
    )
    contract_criteria = {
        (str(item.get("kind") or ""), str(item.get("expected") or "")) for item in spec.criteria
    }
    corroborated_criteria = tuple(
        item
        for item in result.criteria
        if (str(item.get("kind") or ""), str(item.get("observed") or "")) in contract_criteria
    )
    if not (
        result.changed_paths
        or produced_artifacts
        or result.checks
        or corroborated_criteria
        or spec.artifacts
        or spec.checks
        or spec.criteria
    ):
        fail("result_malformed", "typed success contains no verifiable claims")
    for item in result.criteria:
        if item not in corroborated_criteria:
            fail(
                "criteria_unverifiable",
                f"result criterion outside the contract: {item.get('kind') or '<empty>'}: "
                f"{item.get('observed') or '<empty>'}",
            )

    for artifact in spec.artifacts:
        if artifact not in observation.existing_paths:
            fail("artifact_missing", f"declared artifact is absent: {artifact}")

    for path in (*result.changed_paths, *result.artifacts):
        if not _within_scope(path, spec.path_scope):
            fail("path_outside_scope", f"outside the task scope: {path}")

    declared = set(result.changed_paths)
    observed = set(observation.changed_paths)
    for path in sorted(declared - observed):
        fail("diff_missing_declared", f"declared as changed but unchanged: {path}")
    # Undeclared changes are only attributable to this task when the contract
    # bounds what it may touch; without a scope the worktree may legitimately
    # carry changes from work this stage never claimed.
    for path in sorted(observed):
        if _canonical_repo_path(path) is None:
            fail(
                "observed_path_outside_scope" if spec.path_scope else "path_outside_scope",
                f"unsafe observed change path: {path}",
            )
        elif spec.path_scope and not _within_scope(path, spec.path_scope):
            fail(
                "observed_path_outside_scope",
                f"observed change outside the task scope: {path}",
            )
        elif spec.path_scope and path not in declared:
            fail("diff_undeclared_change", f"changed but not declared: {path}")

    declared_checks = {
        str(entry.get("name") or ""): bool(entry.get("passed")) for entry in result.checks
    }
    for entry in result.checks:
        name = str(entry.get("name") or "")
        passed = bool(entry.get("passed"))
        outcome = observation.checks.get(name)
        if outcome is None:
            fail("check_not_run", f"result check never ran: {name}")
        elif passed and not outcome:
            fail("check_failed", f"runtime check failed: {name}")
        if not passed:
            # A successful result cannot include a failed check claim.
            fail("check_failed", f"the result reports {name} as failed")

    for artifact in result.artifacts:
        if artifact not in observation.existing_paths:
            fail("artifact_missing", f"result artifact is absent: {artifact}")

    for name in spec.checks:
        outcome = observation.checks.get(name)
        if outcome is None:
            fail("check_not_run", f"required check never ran: {name}")
            continue
        if not outcome:
            fail("check_failed", f"required check failed: {name}")
            continue
        if name in declared_checks and not declared_checks[name]:
            fail("check_failed", f"the result reports {name} as failed")

    for criterion in spec.criteria:
        outcome = _verify_criterion(criterion, observation)
        if outcome is not None:
            fail(*outcome)

    if failures:
        return Verification(False, tuple(codes), tuple(failures))
    return Verification(True, ("verified",), ())


def verification_gate_check(
    verification: Verification,
    *,
    name: str = "task-verification",
    kind: str = "tests",
    evidence_ref: str = "",
) -> GateCheck:
    """Project a verifier outcome without exposing verifier source or raw output."""
    status = (
        "PASS"
        if verification.verified
        else "UNKNOWN"
        if "not_evaluated" in verification.reason_codes
        else "FAIL"
    )
    return GateCheck(kind, name, status, evidence_ref)  # type: ignore[arg-type]


def observation_gate_checks(
    observation: Observation, *, evidence_prefix: str = "check"
) -> tuple[GateCheck, ...]:
    return tuple(
        GateCheck(
            "tests",
            str(name),
            "PASS" if outcome is True else "FAIL" if outcome is False else "UNKNOWN",
            f"{evidence_prefix}:{name}",
        )
        for name, outcome in sorted(observation.checks.items())
    )


__all__ = [
    "Observation",
    "REASON_CODES",
    "Verification",
    "observation_gate_checks",
    "verification_gate_check",
    "verify",
]
