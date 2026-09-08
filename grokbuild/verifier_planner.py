"""What evidence would settle this? - asked separately from who should work.

The router decides who works. The judge decides what follows from the evidence
already in hand. Neither of them should decide what evidence to go and get,
because that decision is about cost, safety and feasibility rather than about
preference - so it lives here, and it is still only a request.

This module returns a ``NeedEvidence`` description. It does not run the
experiment, spawn anything or touch state; the harness turns the request into
an ``evidence_collection`` stage, obtains the evidence, and re-runs the judge.
Keeping the judge unable to order its own experiments is what stops it from
growing into a second harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

AttributionOutcome = Literal["attributed", "not_attributed", "inconclusive"]

EVIDENCE_KINDS = (
    "discriminating_test",
    "hard_requirement_recheck",
    "artifact_completion",
    "independent_reproduction",
)

MAX_ITEMS = 32
MAX_TEXT = 200


def _bounded_items(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        return ()
    return tuple(str(value or "")[:MAX_TEXT] for value in values[:MAX_ITEMS])


@dataclass(frozen=True)
class ChangeMap:
    """Runtime-owned change and behavior evidence consumed by the planner.

    The harness derives this map from typed runtime evidence. Candidate output
    must never derive or amend it, and this pure module only consumes it.
    """

    affected_paths: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    regression_risks: tuple[str, ...] = ()
    check_inventory: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("affected_paths", "invariants", "regression_risks", "check_inventory"):
            object.__setattr__(self, name, _bounded_items(getattr(self, name)))


@dataclass(frozen=True)
class NeedEvidence:
    """A request for evidence, addressed to the harness."""

    kind: str
    hypothesis: str
    constraints: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "hypothesis": self.hypothesis,
            "constraints": list(self.constraints),
            "requirements": list(self.requirements),
            "checks": list(self.checks),
        }


MIN_TAG_LENGTH = 3


def _match_key(value: str) -> str:
    return "-".join("".join(char if char.isalnum() else " " for char in value.casefold()).split())


def _path_stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _tags(*values: str) -> set[str]:
    """Normalized match tags; a stem too short to name anything yields none."""
    return {key for value in values if len(key := _match_key(value)) >= MIN_TAG_LENGTH}


def _tag_matches(tag: str, check: str) -> bool:
    """True when the tag's words appear contiguously among the check's words.

    Substring matching let a one-letter stem select every check with that
    letter; word boundaries keep attribution to checks that actually name the
    changed thing.
    """
    return f"-{tag}-" in f"-{_match_key(check)}-"


def _targeted_checks(change_map: ChangeMap) -> tuple[str, ...]:
    """Select checks naming a changed path stem, invariant, or risk tag."""
    tags = _tags(
        *(_path_stem(path) for path in change_map.affected_paths),
        *change_map.invariants,
        *change_map.regression_risks,
    )
    return tuple(
        sorted(
            {
                check
                for check in change_map.check_inventory
                if any(_tag_matches(tag, check) for tag in tags)
            }
        )
    )


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _check_outcomes(evidence: object) -> dict[str, bool] | None:
    checks = _field(evidence, "checks")
    if not isinstance(checks, Mapping) or len(checks) > MAX_ITEMS:
        return None
    outcomes: dict[str, bool] = {}
    for name, outcome in checks.items():
        if not isinstance(name, str) or not name or len(name) > MAX_TEXT:
            return None
        if outcome is not True and outcome is not False:
            return None
        outcomes[name] = outcome
    return outcomes


def _affected_check_names(
    baseline: Mapping[str, bool],
    candidate: Mapping[str, bool],
    change_map: ChangeMap,
) -> tuple[str, ...]:
    tags = _tags(*(_path_stem(path) for path in change_map.affected_paths), *change_map.invariants)
    names = {*baseline, *candidate, *change_map.check_inventory}
    return tuple(sorted(name for name in names if any(_tag_matches(tag, name) for tag in tags)))


def attribute_evidence(
    baseline: object,
    candidate: object,
    change_map: ChangeMap | None = None,
) -> AttributionOutcome:
    """Decide whether check evidence attributes an improvement to the change.

    Each input must expose a ``checks`` mapping with at most 32 non-empty string
    names (at most 200 characters each) and strict boolean outcomes. Other
    evidence fields, including aggregate metrics, are ignored. With a change
    map, affected checks are names matching an affected path stem or invariant;
    without one, every check present in both inputs is affected.

    Attribution requires at least one affected candidate pass whose baseline
    outcome is false or absent, with no affected regression or incomplete
    candidate outcome. Identical outcomes and candidate regressions are
    ``not_attributed``; missing or malformed evidence is ``inconclusive``.
    A caller must not invoke a judge for ``not_attributed`` evidence.
    """
    baseline_checks = _check_outcomes(baseline)
    candidate_checks = _check_outcomes(candidate)
    if baseline_checks is None or candidate_checks is None:
        return "inconclusive"

    if change_map is None:
        affected = tuple(sorted(baseline_checks.keys() & candidate_checks.keys()))
    else:
        affected = _affected_check_names(baseline_checks, candidate_checks, change_map)
    if not affected:
        return "inconclusive"

    improved = False
    incomplete = False
    for name in affected:
        baseline_outcome = baseline_checks.get(name)
        candidate_outcome = candidate_checks.get(name)
        if candidate_outcome is None:
            incomplete = True
        elif baseline_outcome is True and candidate_outcome is False:
            return "not_attributed"
        elif candidate_outcome is True and baseline_outcome is not True:
            improved = True
        elif baseline_outcome is None:
            incomplete = True
    if incomplete:
        return "inconclusive"
    return "attributed" if improved else "not_attributed"


def _unsettled_requirements(verdict) -> tuple[str, ...]:
    unsettled: list[str] = []
    for result in verdict.requirement_results:
        if not result.get("hard"):
            continue
        outcomes = [value for key, value in result.items() if key not in {"name", "hard"}]
        if any(value is not True for value in outcomes):
            unsettled.append(str(result.get("name")))
    return tuple(unsettled)


def analyze(
    verdict,
    candidate_labels: Sequence[str] = (),
    change_map: ChangeMap | None = None,
) -> NeedEvidence | None:
    """Return the evidence that would settle a verdict, or None if none would.

    A decided verdict needs nothing. An unjudgeable one needs a valid bundle
    before any test is worth running. An undecided one needs a test that can
    actually separate the candidates - and naming what that test must
    discriminate is the whole point, since a test both candidates pass tells
    the next judging round nothing it did not already know.
    """
    if verdict.verdict in {"a", "b", "tie"}:
        return None
    labels = tuple(candidate_labels)
    checks = _targeted_checks(change_map) if change_map is not None else ()
    if verdict.verdict == "unjudgeable":
        if "identity_leak" in verdict.reason_codes:
            return NeedEvidence(
                "artifact_completion",
                "the bundle carried candidate identity, so no comparison is admissible",
                ("re-extract artifacts with identity fields and identity terms removed",),
                (),
                checks,
            )
        return NeedEvidence(
            "artifact_completion",
            "no comparable artifacts were extracted from the candidates",
            ("collect the declared artifacts for every candidate before judging again",),
            (),
            checks,
        )
    unsettled = _unsettled_requirements(verdict)
    if unsettled:
        return NeedEvidence(
            "hard_requirement_recheck",
            "the candidates are separated by requirements the runtime has not confirmed",
            (
                "run the deterministic check for each unsettled requirement",
                "record the outcome per candidate, not per model",
            ),
            unsettled,
            checks,
        )
    return NeedEvidence(
        "discriminating_test",
        "the judges could not distinguish the candidates on the evidence at hand",
        (
            "the test must be able to fail for at least one candidate",
            "the test must run identically against every candidate",
            f"apply it to: {', '.join(labels) if labels else 'every candidate'}",
        ),
        (),
        checks,
    )


def evidence_stage_spec(request: NeedEvidence, stage_id: str = "evidence-collection") -> dict:
    """Describe the stage the harness should compose for this request.

    Returned as data rather than an ``ExecutionStage`` so this module keeps no
    dependency on the execution layer: composing, ordering and gating the stage
    remain the harness's decisions.
    """
    spec = {
        "role": "verifier-planner",
        "required": True,
        "reason": f"{request.kind}: {request.hypothesis}",
        "kind": "evidence_collection",
        "stage_id": stage_id,
    }
    if request.checks:
        spec["checks"] = list(request.checks)
    return spec


__all__ = [
    "AttributionOutcome",
    "ChangeMap",
    "EVIDENCE_KINDS",
    "NeedEvidence",
    "analyze",
    "attribute_evidence",
    "evidence_stage_spec",
]
