"""Pure per-criterion verdicts between hard checks and pairwise judging.

Hard deterministic checks run first and are outside this module. This layer
mechanically gives one cheap verdict from structured evidence; in production,
the same boundary is where a low-cost model may judge one criterion. Only an
``unknown`` (disputed) criterion escalates to pairwise judges and, if still
unresolved, frontier adjudication.

A hidden-states probe is deliberately not implemented: the available models
are behind APIs, so their hidden representations are unavailable. This is a
per-criterion verdict layer, not a probe. It performs no I/O, routing, spawning,
or state mutation.
"""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass
from typing import Literal, Mapping

Verdict = Literal["pass", "fail", "unknown"]

_MISSING = object()
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


def _canonical_repo_path(path: str) -> str | None:
    if not path or path.startswith("/") or _DRIVE_PATH_RE.match(path):
        return None
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    return getattr(value, name, _MISSING)


def _path_verdict(expected: str, observed: object) -> Verdict:
    if observed is _MISSING or observed is None:
        return "unknown"
    if isinstance(observed, (str, bytes)):
        return "unknown"
    try:
        return "pass" if expected in observed else "fail"
    except (TypeError, ValueError):
        return "unknown"


def judge_criterion(contract_criterion: object, evidence: object) -> Verdict:
    """Return ``pass``, ``fail``, or ``unknown`` for one contract criterion.

    Criteria and evidence are duck-typed. Criteria need ``kind`` and
    ``expected`` fields; evidence may be an Observation-like object or mapping
    with ``existing_paths``, ``changed_paths``, and ``checks`` fields. Missing
    or malformed evidence is disputed rather than failed closed.
    """
    kind = _field(contract_criterion, "kind")
    expected = _field(contract_criterion, "expected")
    if not isinstance(kind, str) or not isinstance(expected, str) or not expected:
        return "unknown"

    if kind in {"path_exists", "path_changed"}:
        expected_path = _canonical_repo_path(expected)
        if expected_path is None:
            return "unknown"
        field = "existing_paths" if kind == "path_exists" else "changed_paths"
        return _path_verdict(expected_path, _field(evidence, field))
    if kind == "check_passed":
        checks = _field(evidence, "checks")
        if not isinstance(checks, Mapping) or expected not in checks:
            return "unknown"
        outcome = checks[expected]
        if outcome is True:
            return "pass"
        if outcome is False:
            return "fail"
        return "unknown"
    return "unknown"


TOKEN_OUTCOMES = ("PASS", "FAIL", "ABSTAIN")
# Contract value for the future request path; live provider requests currently come from the host.
CALIBRATION_TOP_LOGPROBS = 20


@dataclass(frozen=True)
class CriterionTokenVerdict:
    outcome: str
    token: str
    margin: float | None
    threshold: float
    calibrated: bool
    schema_version: int = 1


def _logsumexp(values: list[float]) -> float:
    peak = max(values)
    return peak + math.log(sum(math.exp(value - peak) for value in values))


def judge_criterion_token(
    token: str,
    token_logprobs: Mapping[str, float] | None,
    *,
    calibrated_threshold: float,
) -> CriterionTokenVerdict:
    """Calibrate a forced token from provider logits or abstain cleanly."""
    normalized = str(token).strip().upper()
    try:
        threshold = float(calibrated_threshold)
    except (TypeError, ValueError):
        threshold = math.nan
    if not math.isfinite(threshold) or threshold < 0.0:
        return CriterionTokenVerdict(
            "ABSTAIN_LOGITS_UNAVAILABLE", normalized, None, threshold, False
        )
    if normalized not in TOKEN_OUTCOMES:
        return CriterionTokenVerdict("ABSTAIN_MALFORMED_TOKEN", normalized, None, threshold, False)
    if not token_logprobs:
        return CriterionTokenVerdict(
            "ABSTAIN_LOGITS_UNAVAILABLE", normalized, None, threshold, False
        )
    try:
        aliases: dict[str, list[float]] = {}
        for raw_name, score in token_logprobs.items():
            name = str(raw_name).strip().lstrip("Ġ▁").strip().upper()
            if name in TOKEN_OUTCOMES:
                value = float(score)
                if not math.isfinite(value):
                    raise ValueError("non-finite log probability")
                aliases.setdefault(name, []).append(value)
        scores = {name: _logsumexp(values) for name, values in aliases.items()}
    except (AttributeError, TypeError, ValueError):
        return CriterionTokenVerdict(
            "ABSTAIN_LOGITS_UNAVAILABLE", normalized, None, threshold, False
        )
    if normalized not in scores or len(scores) < 2:
        return CriterionTokenVerdict(
            "ABSTAIN_LOGITS_UNAVAILABLE", normalized, None, threshold, False
        )
    alternatives = [score for name, score in scores.items() if name != normalized]
    margin = scores[normalized] - max(alternatives)
    if normalized == "ABSTAIN" or margin <= threshold:
        return CriterionTokenVerdict("ABSTAIN", normalized, margin, threshold, True)
    return CriterionTokenVerdict(normalized, normalized, margin, threshold, True)


__all__ = [
    "CALIBRATION_TOP_LOGPROBS",
    "CriterionTokenVerdict",
    "TOKEN_OUTCOMES",
    "Verdict",
    "judge_criterion",
    "judge_criterion_token",
]
