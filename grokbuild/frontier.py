"""When is expensive reasoning worth buying? - scored from observable signals.

A model's own `confidence: 0.74` is not evidence. What can be observed instead
is whether independent planners agreed, whether anyone can say how the result
would be checked, how many coupled constraints are in play, how many subsystems
must move together, how novel the task looks, whether agents are giving
mutually exclusive explanations, and whether repair has already failed twice.
Those are the signals this module weighs.

The weights below are a starting guess, and they are meant to be replaced by
ones learned from real task history. That history does not exist yet, so the
mechanism records every escalation decision and its outcome and nothing here
trains on anything: **awaiting escalation dataset**. Shipping a learned router
before the dataset exists would be fitting noise.

Escalation happens *before* execution. Escalating after a bad cheap plan has
already run a swarm has paid for the swarm and polluted the trajectory, which
is the opposite of the point: buy framing, decomposition and invariants from
the expensive model, then let cheap models execute.

This module is pure - it scores and decides, it does not route, spawn or spend.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping

FRONTIER_CONTRACT_VERSION = 1

# Starting coefficients, not tuned parameters. They encode one belief each:
# disagreement between independent decompositions is the strongest available
# evidence that a task is genuinely ambiguous, and a repair loop that has
# already failed twice is the strongest evidence that cheap iteration is not
# converging. AWAITING ESCALATION DATASET - do not hand-tune these; replace them
# with coefficients fitted to recorded outcomes once enough exist.
SIGNAL_WEIGHTS: Mapping[str, float] = {
    "planner_disagreement": 0.30,
    "verification_uncertainty": 0.20,
    "constraint_density": 0.10,
    "subsystem_coupling": 0.10,
    "novelty": 0.10,
    "hypothesis_conflict": 0.10,
    "repair_history": 0.10,
}

LOW_SUPPLY_WEIGHT = 1.5
DEFAULT_THRESHOLD = 0.55


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))


@dataclass(frozen=True)
class FrontierSignals:
    """Observable signals, each normalised to 0..1. None of them is self-report."""

    planner_disagreement: float = 0.0
    verification_uncertainty: float = 0.0
    constraint_density: float = 0.0
    subsystem_coupling: float = 0.0
    novelty: float = 0.0
    hypothesis_conflict: float = 0.0
    repair_history: float = 0.0
    candidate_supply: float | None = None

    def normalised(self) -> dict[str, float]:
        values = {
            item.name: _clamp(getattr(self, item.name))
            for item in fields(self)
            if item.name != "candidate_supply"
        }
        if self.candidate_supply is not None:
            values["candidate_supply_shortfall"] = 1.0 - _clamp(self.candidate_supply)
        return values

    def to_dict(self) -> dict[str, float]:
        return self.normalised()


@dataclass(frozen=True)
class FrontierDecision:
    """The score, the threshold it was compared against, and what dominated it."""

    score: float
    threshold: float
    escalate: bool
    contributions: tuple[tuple[str, float], ...] = ()
    contract_version: int = FRONTIER_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "score": self.score,
            "threshold": self.threshold,
            "escalate": self.escalate,
            "contributions": [
                {"signal": name, "contribution": value} for name, value in self.contributions
            ],
        }


def _scoring_inputs(
    signals: FrontierSignals, weights: Mapping[str, float] | None
) -> tuple[dict[str, float], dict[str, float]]:
    values = signals.normalised()
    if weights is not None:
        known = set(SIGNAL_WEIGHTS) | (
            {"candidate_supply_shortfall"} if signals.candidate_supply is not None else set()
        )
        unknown = sorted(set(weights) - known)
        if unknown:
            raise ValueError(f"unknown frontier weight key(s): {', '.join(unknown)}")
    table = dict(SIGNAL_WEIGHTS if weights is None else weights)
    if signals.candidate_supply is not None and weights is None:
        table["candidate_supply_shortfall"] = LOW_SUPPLY_WEIGHT
    return table, values


def frontier_score(signals: FrontierSignals, weights: Mapping[str, float] | None = None) -> float:
    """Weighted sum of the observable signals, normalised to 0..1."""
    table, values = _scoring_inputs(signals, weights)
    total_weight = sum(abs(weight) for weight in table.values()) or 1.0
    return sum(values.get(name, 0.0) * weight for name, weight in table.items()) / total_weight


def decide(
    signals: FrontierSignals,
    threshold: float = DEFAULT_THRESHOLD,
    weights: Mapping[str, float] | None = None,
) -> FrontierDecision:
    """Score the task and say whether to buy the expensive meta-planner.

    The contributions are returned alongside the score so an operator reading a
    surprising escalation can see which signal drove it, rather than being told
    a number.
    """
    table, values = _scoring_inputs(signals, weights)
    total_weight = sum(abs(weight) for weight in table.values()) or 1.0
    contributions = tuple(
        sorted(
            (
                (name, values.get(name, 0.0) * weight / total_weight)
                for name, weight in table.items()
            ),
            key=lambda item: (-item[1], item[0]),
        )
    )
    score = sum(value for _name, value in contributions)
    return FrontierDecision(score, threshold, score >= threshold, contributions)


__all__ = [
    "DEFAULT_THRESHOLD",
    "FRONTIER_CONTRACT_VERSION",
    "FrontierDecision",
    "FrontierSignals",
    "SIGNAL_WEIGHTS",
    "decide",
    "frontier_score",
]
