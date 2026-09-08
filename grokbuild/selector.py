"""Choose among candidate results, and measure whether choosing helped.

Selection is a separate concern from judging: the judge compares a pair and
reports what the evidence supports; the selector walks a field of candidates,
lets deterministic verification eliminate what it can, and only then spends
comparisons. It is a pure function of the candidates and the verdicts it is
given - it never spawns a judge or runs a check itself.

The metric that matters is not how good the judge looks in isolation. A judge
with a fine win-rate is worthless if the portfolio never contained anything
better than the single best model would have produced anyway. What is worth
maximising is the share of the available headroom the selector actually
captures.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Callable, Sequence

from grokbuild.artifact_judge import Candidate, ComparisonAggregate, TaskContract, Verdict, compare
from grokbuild.deterministic_gate import decide_gate, legacy_checks


# AWAITING JUDGE-CALIBRATION DATASET (do not train).
CONSENSUS_OVERTURN_CONFIDENCE = 0.9


@dataclass(frozen=True)
class ArtifactCluster:
    """Equivalent candidate artifacts and their deterministic representative."""

    members: tuple[str, ...]
    representative: str
    support: int
    content_hash: str = ""


@dataclass(frozen=True)
class Selection:
    """The selector's answer, with the reason it stopped where it did."""

    selected: str = ""
    reason: str = ""
    eliminated: tuple[str, ...] = ()
    comparisons: int = 0
    unresolved: tuple[str, ...] = ()

    @property
    def decided(self) -> bool:
        return bool(self.selected)

    def to_dict(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "reason": self.reason,
            "eliminated": list(self.eliminated),
            "comparisons": self.comparisons,
            "unresolved": list(self.unresolved),
        }


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_SUFFIXES = (".diff", ".patch")


def _normalize_body(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line.strip())


def _indent_width(line: str) -> int:
    expanded = line.expandtabs(8)
    return len(expanded) - len(expanded.lstrip(" "))


def _normalize_line(line: str) -> str:
    """Normalize one diff line: inner whitespace collapses, indentation width stays."""
    body = _normalize_body(line)
    return " " * _indent_width(line) + body if body else ""


def _normalize_file_lines(lines: Sequence[str]) -> tuple[str, ...]:
    """Normalize a whole file: inner whitespace collapses, indentation keeps its level.

    Levels follow the file's own nesting, the way a tokenizer emits INDENT and
    DEDENT, so a two-space and an eight-space rendering of the same structure
    agree while a line moved to another block does not. Erasing indentation
    outright let a candidate with a different block structure join a
    consensus cluster in Python, YAML or Makefile artifacts.
    """
    stack = [0]
    normalized: list[str] = []
    for line in lines:
        body = _normalize_body(line)
        if not body:
            normalized.append("")
            continue
        width = _indent_width(line)
        if width > stack[-1]:
            stack.append(width)
        else:
            while len(stack) > 1 and width < stack[-1]:
                stack.pop()
            if width > stack[-1]:
                # A dedent to a column the file never opened is its own level.
                stack.append(width)
        normalized.append(" " * (len(stack) - 1) + body)
    return tuple(normalized)


def _normalized_artifact(path: str, content: str) -> tuple[object, ...]:
    """Normalize whole files structurally and declared diffs by positioned changes."""
    lines = content.splitlines()
    if not path.lower().endswith(_DIFF_SUFFIXES):
        return ("file", *_normalize_file_lines(lines))

    changes: list[tuple[int, int, str, str]] = []
    old_line = new_line = 0
    old_remaining = new_remaining = 0
    in_hunk = False
    found_hunk = False
    for line in lines:
        header = _HUNK_HEADER.match(line)
        if header:
            old_line = int(header.group(1))
            old_remaining = int(header.group(2) or 1)
            new_line = int(header.group(3))
            new_remaining = int(header.group(4) or 1)
            in_hunk = True
            found_hunk = True
            continue
        if in_hunk and old_remaining == 0 and new_remaining == 0:
            in_hunk = False
        if not in_hunk or line.startswith("\\"):
            continue
        if line.startswith("+"):
            changes.append((old_line, new_line, "+", _normalize_line(line[1:])))
            new_line += 1
            new_remaining = max(0, new_remaining - 1)
        elif line.startswith("-"):
            changes.append((old_line, new_line, "-", _normalize_line(line[1:])))
            old_line += 1
            old_remaining = max(0, old_remaining - 1)
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
            old_remaining = max(0, old_remaining - 1)
            new_remaining = max(0, new_remaining - 1)
    if not found_hunk:
        return ("diff-text", *(_normalize_line(line) for line in lines))
    return ("diff", *sorted(changes))


def _artifact_key(candidate: Candidate, requirement_names: Sequence[str]) -> tuple[object, ...]:
    artifacts = tuple(
        sorted(
            (path, _normalized_artifact(path, content))
            for path, content in candidate.artifacts.items()
        )
    )
    hard_results = tuple(
        (name, candidate.hard_results.get(name)) for name in sorted(set(requirement_names))
    )
    return artifacts, hard_results


def _content_hash(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _candidate_content_hash(candidate: Candidate) -> str:
    return _content_hash(tuple(sorted(candidate.artifacts.items())))


def _hard_requirement_names(
    requirements: TaskContract | Sequence[str] | None,
) -> tuple[str, ...]:
    if isinstance(requirements, TaskContract):
        return tuple(requirement.name for requirement in requirements.hard_requirements)
    return tuple(requirements or ())


def cluster_artifacts(
    bundles: Sequence[Candidate],
    requirements: TaskContract | Sequence[str] | None = None,
) -> tuple[ArtifactCluster, ...]:
    """Partition candidates by normalized artifacts and declared hard outcomes."""
    requirement_names = _hard_requirement_names(requirements)
    partitions: dict[tuple[object, ...], list[Candidate]] = {}
    for candidate in bundles:
        key = _artifact_key(candidate, requirement_names)
        partitions.setdefault(key, []).append(candidate)

    clusters = []
    for key, candidates in partitions.items():
        members = tuple(sorted(candidate.label for candidate in candidates))
        representative = min(
            candidates,
            key=lambda candidate: (_candidate_content_hash(candidate), candidate.label),
        ).label
        clusters.append(ArtifactCluster(members, representative, len(members), _content_hash(key)))
    return tuple(sorted(clusters, key=lambda cluster: cluster.content_hash))


def _gate_candidates(contract: TaskContract, candidates: Sequence[Candidate]):
    required = tuple(requirement.name for requirement in contract.hard_requirements)
    return decide_gate(
        {
            candidate.label: (
                legacy_checks(required, candidate.hard_results) + candidate.gate_checks
            )
            for candidate in candidates
        }
    )


def _hard_failures(contract: TaskContract, candidate: Candidate) -> tuple[str, ...]:
    gate = _gate_candidates(contract, (candidate,))
    return tuple(check.name for item in gate.candidates for check in item.checks if check.status == "FAIL")


def select(
    contract: TaskContract,
    candidates: Sequence[Candidate],
    judge: Callable[[Candidate, Candidate], Verdict] | None = None,
    identity_terms: Sequence[str] = (),
) -> Selection:
    """Eliminate on deterministic evidence first, then compare what survives.

    Comparisons are the expensive part, so nothing is compared until hard
    verification has removed everything it can. When the survivors cannot be
    separated the selector says so instead of picking one: an unresolved field
    is a request for a discriminating test, not a coin toss.
    """
    if not candidates:
        return Selection(reason="no candidates were produced")

    gate = _gate_candidates(contract, candidates)
    eliminated = list(gate.rejected)
    if gate.unknown:
        return Selection(
            reason="deterministic evidence is unknown",
            eliminated=tuple(eliminated),
            unresolved=gate.unknown,
        )
    survivors = [candidate for candidate in candidates if candidate.label in gate.eligible]
    if not survivors:
        return Selection(
            reason="every candidate failed a hard requirement",
            eliminated=tuple(eliminated),
            unresolved=tuple(candidate.label for candidate in candidates),
        )
    if len(survivors) == 1:
        return Selection(
            selected=survivors[0].label,
            reason="the only candidate that satisfied every hard requirement",
            eliminated=tuple(eliminated),
        )

    decide = judge or (lambda a, b: compare(contract, a, b, (), identity_terms))
    leader = survivors[0]
    comparisons = 0
    for challenger in survivors[1:]:
        verdict = decide(leader, challenger)
        comparisons += 1
        if verdict.verdict == "b":
            leader = challenger
            continue
        if verdict.verdict == "a":
            continue
        return Selection(
            reason=f"comparison was inconclusive: {verdict.verdict}",
            eliminated=tuple(eliminated),
            comparisons=comparisons,
            unresolved=tuple(candidate.label for candidate in survivors),
        )
    return Selection(
        selected=leader.label,
        reason="preferred over every other surviving candidate",
        eliminated=tuple(eliminated),
        comparisons=comparisons,
    )


def _ordered_preference(
    first: Candidate,
    second: Candidate,
    forward: Verdict,
    reverse: Verdict,
) -> tuple[Candidate | None, float]:
    forward_winner = first if forward.verdict == "a" else second if forward.verdict == "b" else None
    reverse_winner = second if reverse.verdict == "a" else first if reverse.verdict == "b" else None
    if forward_winner is None or forward_winner is not reverse_winner:
        return None, 0.0
    return forward_winner, min(forward.confidence, reverse.confidence)


def select_v2(
    contract: TaskContract,
    candidates: Sequence[Candidate],
    judge: Callable[[Candidate, Candidate], Verdict],
) -> Selection:
    """Select by hard evidence, artifact consensus, then reversed-order judging."""
    if not candidates:
        return Selection(reason="no candidates were produced")

    gate = _gate_candidates(contract, candidates)
    eliminated = gate.rejected
    if gate.unknown:
        return Selection(
            reason="deterministic evidence is unknown",
            eliminated=eliminated,
            unresolved=gate.unknown,
        )
    survivors = [candidate for candidate in candidates if candidate.label in gate.eligible]
    if not survivors:
        return Selection(
            reason="every candidate failed a hard requirement",
            eliminated=eliminated,
            unresolved=tuple(candidate.label for candidate in candidates),
        )

    clusters = cluster_artifacts(survivors, contract)
    by_label = {candidate.label: candidate for candidate in survivors}
    ordered = sorted(clusters, key=lambda cluster: (-cluster.support, cluster.content_hash))
    if len(ordered) == 1 or ordered[0].support > ordered[1].support:
        return Selection(
            selected=ordered[0].representative,
            reason="one artifact-equivalence cluster has uniquely maximal support",
            eliminated=eliminated,
        )

    leader = ordered[0]
    comparisons = 0
    for challenger in ordered[1:]:
        first = by_label[leader.representative]
        second = by_label[challenger.representative]
        forward = judge(first, second)
        reverse = judge(second, first)
        comparisons += 2
        preferred, confidence = _ordered_preference(first, second, forward, reverse)
        if preferred is None:
            return Selection(
                reason="reversed-order judges did not unanimously prefer one representative",
                eliminated=eliminated,
                comparisons=comparisons,
                unresolved=tuple(cluster.representative for cluster in ordered),
            )
        preferred_cluster = leader if preferred is first else challenger
        other_cluster = challenger if preferred is first else leader
        if (
            preferred_cluster.support < other_cluster.support
            and confidence < CONSENSUS_OVERTURN_CONFIDENCE
        ):
            return Selection(
                reason="judge confidence was too low to overturn larger consensus support",
                eliminated=eliminated,
                comparisons=comparisons,
                unresolved=(leader.representative, challenger.representative),
            )
        leader = preferred_cluster

    return Selection(
        selected=leader.representative,
        reason="consensus prior resolved by unanimous reversed-order judging",
        eliminated=eliminated,
        comparisons=comparisons,
    )


def select_pairwise_aggregates(
    contract: TaskContract,
    candidates: Sequence[Candidate],
    compare_pair: Callable[[Candidate, Candidate], ComparisonAggregate],
) -> Selection:
    """Select using comparison aggregates only; never score and sort candidates."""
    if not candidates:
        return Selection(reason="no candidates were produced")
    gate = _gate_candidates(contract, candidates)
    if gate.unknown:
        return Selection(
            reason="deterministic evidence is unknown; more evidence required",
            eliminated=gate.rejected,
            unresolved=gate.unknown,
        )
    survivors = [item for item in candidates if item.label in gate.eligible]
    if not survivors:
        return Selection(
            reason="every candidate failed deterministic verification",
            eliminated=gate.rejected,
            unresolved=tuple(item.label for item in candidates),
        )
    if len(survivors) == 1:
        return Selection(survivors[0].label, "only deterministic survivor", gate.rejected)
    leader = survivors[0]
    comparisons = 0
    for challenger in survivors[1:]:
        aggregate = compare_pair(leader, challenger)
        comparisons += len(aggregate.reads)
        if aggregate.final == "A":
            continue
        if aggregate.final == "B":
            leader = challenger
            continue
        return Selection(
            reason="pairwise aggregate abstained; discriminating evidence required",
            eliminated=gate.rejected,
            comparisons=comparisons,
            unresolved=(leader.label, challenger.label),
        )
    return Selection(
        leader.label,
        "won every pairwise aggregate",
        gate.rejected,
        comparisons,
    )


def oracle_regret(
    selector_success: float,
    best_single_success: float,
    portfolio_oracle_success: float,
) -> float:
    """Return oracle gain minus selector gain for the same portfolio baseline."""
    selector_gain = selector_success - best_single_success
    oracle_gain = portfolio_oracle_success - best_single_success
    return oracle_gain - selector_gain


def selector_capture(
    selector_success: float,
    best_single_success: float,
    portfolio_oracle_success: float,
) -> float | None:
    """Return the share of available headroom the selector captured.

    ``selector_gain`` is what selection added over simply always using the best
    single model; ``oracle_gain`` is what a perfect chooser could have added
    from the same portfolio. Their ratio is the honest question. When the
    portfolio held no headroom at all the ratio is undefined - reporting a
    number there would flatter or punish the selector for something it had no
    influence over.
    """
    oracle_gain = portfolio_oracle_success - best_single_success
    if oracle_gain <= 0:
        return None
    return (selector_success - best_single_success) / oracle_gain


__all__ = [
    "CONSENSUS_OVERTURN_CONFIDENCE",
    "ArtifactCluster",
    "Selection",
    "cluster_artifacts",
    "oracle_regret",
    "select",
    "select_v2",
    "select_pairwise_aggregates",
    "selector_capture",
]
