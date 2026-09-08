"""Artifact-only judging: a pure comparison function, nothing more.

The judge answers one question - "does the evidence show A is preferable to
B?" - and has no other power. It never spawns, never runs tools, never chooses
a model, never decomposes a task and holds no trajectory state. Everything it
needs arrives as arguments, and its answer is a verdict the harness acts on.

Three rules make the answer worth having:

* **Judges select artifacts, never agents.** The bundle a judge sees carries no
  model, provider, agent or author name, no raw assistant response, no
  reasoning trace and no candidate's own explanation. If identity leaks into
  the bundle the comparison is abandoned, not attempted.
* **Deterministic verification dominates.** Hard requirements are compared
  first, in the contract's own order, and a hard failure cannot be outvoted by
  any subjective preference. This is lexicographic precedence, not a weighted
  average.
* **Uncertainty is reported, not resolved.** Two cheap judges see the pair in
  opposite orders; when they disagree, or when either abstains, the answer is
  "this needs a discriminating test", never a guess. Designing that test is the
  verifier-planner's job and running it is the harness's - the judge only says
  what it could not distinguish.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from grokbuild.deterministic_gate import GateCheck, decide_gate, legacy_checks
from grokbuild.judge_evidence import (
    EvidenceFact,
    FORBIDDEN_EVIDENCE_FIELDS,
    JudgeEvidenceBundle,
    bounded_traces,
    forbidden_fields,
)
from grokbuild.redact import fold_confusables

VERDICTS = ("a", "b", "tie", "needs_discriminating_test", "unjudgeable")

REASON_CODES = (
    "hard_requirement_failure",
    "hard_requirement_advantage",
    "judges_agree",
    "judges_disagree",
    "judge_abstained",
    "order_not_reversed",
    "identity_leak",
    "bundle_incomplete",
    "no_candidates",
    "deterministic_reject",
    "deterministic_unknown",
    "reject_all",
    "low_margin",
)

# Component aliases shorter than this ("pro", "max", "std") are too common to
# be identity on their own; a compact term shorter than this is matched only
# as a whole word rather than as a substring.
MIN_ALIAS_LENGTH = 4
MIN_SUBSTRING_LENGTH = 6
# Tier and variant words shared across vendors identify nobody on their own.
GENERIC_ALIASES = frozenset(
    {
        "base",
        "chat",
        "fast",
        "flash",
        "high",
        "instruct",
        "large",
        "latest",
        "lite",
        "medium",
        "mini",
        "nano",
        "plus",
        "preview",
        "small",
        "thinking",
        "turbo",
        "ultra",
    }
)

# Field names that carry identity or the candidate's own narrative. They are
# dropped from the bundle rather than trusted to be ignored.
IDENTITY_FIELDS = frozenset(
    {
        "agent",
        "author",
        "committer",
        "explanation",
        "identity",
        "model",
        "plan",
        "provider",
        "rationale",
        "reasoning",
        "response",
        "trajectory",
        "verdict",
    }
)


@dataclass(frozen=True)
class Requirement:
    """One contract requirement. Hard requirements are verified, not judged."""

    name: str
    hard: bool = True


@dataclass(frozen=True)
class TaskContract:
    """The canonical statement of the task, shared by both candidates."""

    task_id: str
    summary: str = ""
    requirements: tuple[Requirement, ...] = ()

    @property
    def hard_requirements(self) -> tuple[Requirement, ...]:
        return tuple(item for item in self.requirements if item.hard)


@dataclass(frozen=True)
class Candidate:
    """One anonymous candidate: artifacts plus runtime-produced evidence."""

    label: str
    artifacts: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    hard_results: Mapping[str, bool | None] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    gate_checks: tuple[GateCheck, ...] = ()
    evidence_results: tuple[Mapping[str, object], ...] = ()
    traces: tuple[str, ...] = ()


Bundle = JudgeEvidenceBundle


@dataclass(frozen=True)
class JudgeOpinion:
    """One judge's answer about an ordered pair.

    ``prefers`` names the label the judge preferred, or is empty when the judge
    could not tell. ``first``/``second`` record the order the pair was shown in,
    which is what makes reversed-order judging checkable.
    """

    first: str
    second: str
    prefers: str = ""
    confidence: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    invocation_id: str = ""
    response_schema: str = "judge-opinion-v2"

    @property
    def abstained(self) -> bool:
        return self.prefers.casefold() in {"", "abstain", "tie"}


@dataclass(frozen=True)
class ComparisonAggregate:
    """The measured unit for one blind, position-symmetric comparison."""

    comparison_id: str
    candidate_a: str
    candidate_b: str
    base_reads: tuple[JudgeOpinion, ...]
    additional_reads: tuple[JudgeOpinion, ...] = ()
    evidence_round: int = 0
    final: str = "ABSTAIN"
    margin: float = 0.0
    position_bias: float = 0.0
    repeatability: float = 0.0
    schema_version: int = 1

    @property
    def reads(self) -> tuple[JudgeOpinion, ...]:
        return self.base_reads + self.additional_reads

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "comparison_id": self.comparison_id,
            "candidate_a": self.candidate_a,
            "candidate_b": self.candidate_b,
            "base_reads": [vars(item) for item in self.base_reads],
            "additional_reads": [vars(item) for item in self.additional_reads],
            "evidence_round": self.evidence_round,
            "final": self.final,
            "margin": self.margin,
            "position_bias": self.position_bias,
            "repeatability": self.repeatability,
        }


@dataclass(frozen=True)
class Verdict:
    """The strict, evidence-pointing answer."""

    verdict: str
    confidence: float = 0.0
    requirement_results: tuple[dict[str, object], ...] = ()
    hard_failures: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    decisive_evidence_refs: tuple[str, ...] = ()
    discriminating_test: str = ""
    eligible: tuple[str, ...] = ()
    rejected_candidates: tuple[str, ...] = ()
    deterministic_status: str = ""
    comparison_id: str = ""
    aggregate: ComparisonAggregate | None = None
    schema_version: int = 2

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "requirement_results": [dict(item) for item in self.requirement_results],
            "hard_failures": list(self.hard_failures),
            "reason_codes": list(self.reason_codes),
            "decisive_evidence_refs": list(self.decisive_evidence_refs),
            "discriminating_test": self.discriminating_test,
            "eligible": list(self.eligible),
            "rejected_candidates": list(self.rejected_candidates),
            "deterministic_status": self.deterministic_status,
            "comparison_id": self.comparison_id,
            "aggregate": self.aggregate.to_dict() if self.aggregate else None,
        }


def _identity_tokens(value: str) -> tuple[str, ...]:
    """Alphanumeric words of a value after Unicode and confusable folding."""
    folded = fold_confusables(unicodedata.normalize("NFKC", str(value))).casefold()
    return tuple(re.findall(r"[^\W_]+", folded))


def _identity_form(value: str) -> str:
    return "".join(_identity_tokens(value))


def _contains_words(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    width = len(needle)
    if not width or width > len(haystack):
        return False
    return any(
        tuple(haystack[index : index + width]) == tuple(needle)
        for index in range(len(haystack) - width + 1)
    )


@dataclass(frozen=True)
class _LeakMatcher:
    """How one identity term is recognised inside judge-visible text.

    The whole term matches on word boundaries (``gpt-5.6-luna`` in
    ``uses sample luna``), and as a compact substring when it is long enough
    to be unmistakable, which also catches separator evasion. Its component
    aliases (``luna``) match only as whole words: ``lunar_phase`` is not a
    provider, and ``grokbuild`` is not ``grok``.
    """

    term: str
    words: tuple[str, ...]
    compact: str
    aliases: tuple[str, ...]

    def found_in(self, words: Sequence[str], compact: str) -> bool:
        if self.words and _contains_words(words, self.words):
            return True
        if len(self.compact) >= MIN_SUBSTRING_LENGTH and self.compact in compact:
            return True
        return any(alias in words for alias in self.aliases)


def _leak_matchers(terms: Sequence[str]) -> tuple[_LeakMatcher, ...]:
    matchers: list[_LeakMatcher] = []
    for term in terms:
        words = _identity_tokens(term)
        if not words:
            continue
        aliases = tuple(
            dict.fromkeys(
                word
                for word in words
                if len(word) >= MIN_ALIAS_LENGTH
                and word not in GENERIC_ALIASES
                and word != "".join(words)
            )
        )
        matchers.append(_LeakMatcher(str(term).strip(), words, "".join(words), aliases))
    return tuple(matchers)


def _identity_leak(probe: str, matchers: Sequence[_LeakMatcher]) -> str:
    if not matchers:
        return ""
    words = _identity_tokens(probe)
    compact = "".join(words)
    return next((matcher.term for matcher in matchers if matcher.found_in(words, compact)), "")


def _escape_artifact_payload(value: str) -> str:
    """Keep the bundle's own framing markers inert inside payload text.

    Only the markers are encoded: the judge must read code as the author
    wrote it, so ``<`` and ``>`` (comparisons, generics, HTML) stay literal.
    """
    return (
        str(value).replace("[ARTIFACT", "\\u005bARTIFACT").replace("[/ARTIFACT", "\\u005b/ARTIFACT")
    )


def sanitize(
    candidate: Candidate, identity_terms: Sequence[str] = ()
) -> tuple[Bundle, tuple[str, ...]]:
    """Return the judge-visible bundle and any identity leaks found in it.

    Dropping identity-bearing fields is not enough on its own: a provider name
    quoted inside an artifact would reintroduce exactly the bias the bundle
    exists to remove, so every judge-visible field is checked and the leak is
    reported rather than scrubbed.
    """
    leaks: list[str] = []
    matchers = _leak_matchers(identity_terms)

    label_leak = _identity_leak(candidate.label, matchers)
    if label_leak:
        leaks.append(f"identity term in label: {label_leak}")
    for index, evidence_ref in enumerate(candidate.evidence_refs):
        evidence_leak = _identity_leak(evidence_ref, matchers)
        if evidence_leak:
            leaks.append(f"identity term in evidence_ref[{index}]: {evidence_leak}")

    artifacts: list[tuple[str, str]] = []
    for path in sorted(candidate.artifacts):
        content = candidate.artifacts[path]
        normalized_path = path.casefold().replace("-", "_")
        if path.casefold() in IDENTITY_FIELDS or normalized_path in FORBIDDEN_EVIDENCE_FIELDS:
            leaks.append(f"forbidden artifact name: {path}")
            continue
        for probe in (path, content):
            artifact_leak = _identity_leak(probe, matchers)
            if artifact_leak:
                leaks.append(f"identity term in {path}: {artifact_leak}")
                break
        # Keep structural marker spellings inert inside both payload fields.
        safe_path = _escape_artifact_payload(path)
        safe_content = _escape_artifact_payload(content)
        artifacts.append((safe_path, f"[ARTIFACT path={safe_path}]\n{safe_content}\n[/ARTIFACT]"))
    for key, value in candidate.metadata.items():
        normalized = key.casefold().replace("-", "_")
        if key.casefold() in IDENTITY_FIELDS or normalized in FORBIDDEN_EVIDENCE_FIELDS:
            detail = f"={value}" if str(value) else ""
            leaks.append(f"forbidden metadata field: {key}{detail}")
    facts: list[EvidenceFact] = []
    for index, item in enumerate(candidate.evidence_results):
        if not isinstance(item, Mapping):
            leaks.append(f"malformed evidence_result[{index}]")
            continue
        forbidden = forbidden_fields(item)
        if forbidden:
            leaks.append(f"forbidden evidence_result[{index}] fields: {', '.join(forbidden)}")
            continue
        kind = str(item.get("kind") or "")
        name = str(item.get("name") or "")
        status = str(item.get("status") or "")
        if not kind or not name or not status:
            leaks.append(f"malformed evidence_result[{index}]")
            continue
        evidence_ref = str(item.get("evidence_ref") or "")
        for field, value in (("kind", kind), ("name", name), ("status", status), ("evidence_ref", evidence_ref)):
            evidence_leak = _identity_leak(value, matchers)
            if evidence_leak:
                leaks.append(f"identity term in evidence_result[{index}].{field}: {evidence_leak}")
        facts.append(EvidenceFact(kind, name, status, evidence_ref))
    traces = bounded_traces(candidate.traces)
    for index, trace in enumerate(traces):
        trace_leak = _identity_leak(trace, matchers)
        if trace_leak:
            leaks.append(f"identity term in trace[{index}]: {trace_leak}")
    return (
        Bundle(
            candidate.label,
            tuple(artifacts),
            tuple(facts),
            traces,
            tuple(candidate.evidence_refs),
        ),
        tuple(dict.fromkeys(leaks)),
    )


def _requirement_results(
    contract: TaskContract, a: Candidate, b: Candidate
) -> tuple[dict[str, object], ...]:
    results: list[dict[str, object]] = []
    for requirement in contract.requirements:
        results.append(
            {
                "name": requirement.name,
                "hard": requirement.hard,
                a.label: a.hard_results.get(requirement.name),
                b.label: b.hard_results.get(requirement.name),
            }
        )
    return tuple(results)


def hard_precedence(
    contract: TaskContract, a: Candidate, b: Candidate
) -> tuple[str, tuple[str, ...]]:
    """Compare hard requirements lexicographically, in the contract's order.

    Returns ``(winner_label_or_empty, hard_failures)``. The first requirement
    that separates the candidates decides; nothing later and nothing subjective
    can overturn it. A requirement neither candidate satisfies is a failure for
    both and does not separate them.
    """
    failures: list[str] = []
    winner = ""
    for requirement in contract.hard_requirements:
        left = a.hard_results.get(requirement.name)
        right = b.hard_results.get(requirement.name)
        if left is not True:
            failures.append(f"{a.label}:{requirement.name}")
        if right is not True:
            failures.append(f"{b.label}:{requirement.name}")
        if winner:
            continue
        if left is True and right is not True:
            winner = a.label
        elif right is True and left is not True:
            winner = b.label
    return winner, tuple(failures)


def fold_opinions(
    a: Candidate, b: Candidate, opinions: Sequence[JudgeOpinion]
) -> tuple[str, float, tuple[str, ...]]:
    """Fold independent reversed-order opinions into one answer.

    Two judges see the same pair in opposite orders. Agreement on a candidate is
    a real preference; agreement on a *position* is position bias and shows up
    here as disagreement about the candidate, which is precisely what should
    stop the comparison and ask for a discriminating test. Position bias can
    only show up when both orders were actually judged, so a single opinion,
    or several in the same order, is not an answer either.
    """
    if not opinions:
        return "needs_discriminating_test", 0.0, ("judge_abstained",)
    if any(opinion.abstained for opinion in opinions):
        return "needs_discriminating_test", 0.0, ("judge_abstained",)
    labels = {opinion.prefers for opinion in opinions}
    orders = {(opinion.first, opinion.second) for opinion in opinions}
    pair = {(a.label, b.label), (b.label, a.label)}
    if labels - {a.label, b.label} or orders - pair:
        return "needs_discriminating_test", 0.0, ("bundle_incomplete",)
    if orders != pair:
        return "needs_discriminating_test", 0.0, ("order_not_reversed",)
    if len(labels) != 1:
        return "needs_discriminating_test", 0.0, ("judges_disagree",)
    preferred = labels.pop()
    confidence = min(opinion.confidence for opinion in opinions)
    return ("a" if preferred == a.label else "b"), confidence, ("judges_agree",)


def aggregate_opinions(
    a: Candidate,
    b: Candidate,
    opinions: Sequence[JudgeOpinion],
    *,
    comparison_id: str = "",
    extra_reads_max: int = 3,
    abstain_margin: float = 0.2,
    evidence_round: int = 0,
) -> ComparisonAggregate:
    """Aggregate AB+BA plus at most three independent blind rereads."""
    maximum = 2 + max(0, min(3, int(extra_reads_max)))
    reads = tuple(opinions[:maximum])
    base = reads[:2]
    extra = reads[2:]
    valid_orders = {(a.label, b.label), (b.label, a.label)}
    complete_base = len(base) == 2 and {x[:2] for x in ((op.first, op.second) for op in base)} == valid_orders
    votes = [op.prefers for op in reads if not op.abstained and op.prefers in {a.label, b.label}]
    count_a = votes.count(a.label)
    count_b = votes.count(b.label)
    total = count_a + count_b
    margin = abs(count_a - count_b) / total if total else 0.0
    first_votes = [op.prefers for op in reads if op.first == a.label and not op.abstained]
    second_votes = [op.prefers for op in reads if op.first == b.label and not op.abstained]
    position_bias = 0.0
    if first_votes and second_votes:
        first_position = sum(op.prefers == op.first for op in reads if not op.abstained) / len(votes)
        second_position = sum(op.prefers == op.second for op in reads if not op.abstained) / len(votes)
        position_bias = abs(first_position - second_position)
    repeatability = max(count_a, count_b) / total if total else 0.0
    if not complete_base or any(op.abstained for op in base) or not total or margin <= abstain_margin:
        final = "ABSTAIN"
    else:
        final = "A" if count_a > count_b else "B" if count_b > count_a else "ABSTAIN"
    return ComparisonAggregate(
        comparison_id or f"{a.label}:{b.label}:{evidence_round}",
        a.label,
        b.label,
        base,
        extra,
        evidence_round,
        final,
        margin,
        position_bias,
        repeatability,
    )


def _candidate_checks(contract: TaskContract, candidate: Candidate) -> tuple[GateCheck, ...]:
    adapted = legacy_checks(
        (requirement.name for requirement in contract.hard_requirements),
        candidate.hard_results,
    )
    return adapted + candidate.gate_checks


def compare(
    contract: TaskContract,
    a: Candidate,
    b: Candidate,
    opinions: Sequence[JudgeOpinion] = (),
    identity_terms: Sequence[str] = (),
    *,
    comparison_id: str = "",
    extra_reads_max: int = 3,
    abstain_margin: float = 0.2,
    evidence_round: int = 0,
) -> Verdict:
    """Gate candidates first, then compare only deterministic survivors."""
    gate = decide_gate({a.label: _candidate_checks(contract, a), b.label: _candidate_checks(contract, b)})
    results = _requirement_results(contract, a, b)
    failures = tuple(
        f"{item.label}:{check.name}"
        for item in gate.candidates
        for check in item.checks
        if check.status == "FAIL"
    )
    common = {
        "requirement_results": results,
        "hard_failures": failures,
        "decisive_evidence_refs": tuple(dict.fromkeys((*a.evidence_refs, *b.evidence_refs))),
        "eligible": gate.eligible,
        "rejected_candidates": gate.rejected,
        "deterministic_status": gate.outcome,
        "comparison_id": comparison_id,
    }
    if gate.outcome == "reject_all":
        return Verdict("reject_all", 1.0, reason_codes=("deterministic_reject", "reject_all"), **common)
    if gate.outcome == "abstain":
        return Verdict(
            "needs_discriminating_test",
            0.0,
            reason_codes=("deterministic_unknown",),
            **common,
        )
    if gate.outcome == "select_survivor":
        winner = gate.eligible[0]
        return Verdict(
            "a" if winner == a.label else "b",
            1.0,
            reason_codes=("deterministic_reject", "hard_requirement_advantage"),
            **common,
        )

    bundle_a, leaks_a = sanitize(a, identity_terms)
    bundle_b, leaks_b = sanitize(b, identity_terms)
    if leaks_a or leaks_b:
        return Verdict("unjudgeable", 0.0, reason_codes=("identity_leak",), **common)
    if not bundle_a.artifacts and not bundle_b.artifacts:
        return Verdict("unjudgeable", 0.0, reason_codes=("bundle_incomplete",), **common)

    aggregate = aggregate_opinions(
        a,
        b,
        opinions,
        comparison_id=comparison_id,
        extra_reads_max=extra_reads_max,
        abstain_margin=abstain_margin,
        evidence_round=evidence_round,
    )
    if aggregate.final == "A":
        verdict, codes = "a", ("judges_agree",)
    elif aggregate.final == "B":
        verdict, codes = "b", ("judges_agree",)
    else:
        verdict = "needs_discriminating_test"
        votes = [op.prefers for op in aggregate.reads if not op.abstained]
        codes = (
            ("judges_disagree",)
            if votes.count(a.label) == votes.count(b.label)
            else ("low_margin",)
        )
        if any(op.abstained for op in aggregate.base_reads):
            codes = ("judge_abstained",)
    confidence = (
        min(op.confidence for op in aggregate.reads if not op.abstained)
        if verdict in {"a", "b"}
        else 0.0
    )
    return Verdict(
        verdict,
        confidence,
        reason_codes=codes,
        aggregate=aggregate,
        **common,
    )


__all__ = [
    "Bundle",
    "Candidate",
    "ComparisonAggregate",
    "IDENTITY_FIELDS",
    "JudgeOpinion",
    "REASON_CODES",
    "Requirement",
    "TaskContract",
    "VERDICTS",
    "Verdict",
    "aggregate_opinions",
    "compare",
    "fold_opinions",
    "hard_precedence",
    "sanitize",
]
