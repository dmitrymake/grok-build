"""Deterministic, zero-provider-call composition snapshots for R1."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

from grokbuild.compose import compose_execution, load_barrier_lenses
from grokbuild.policy import Profile

VARIANTS = (
    "single-best-agent",
    "full-pipeline",
    "no-recon",
    "no-LLM-review",
    "verifier-only",
    "same-provider-reviewer",
    "cross-provider-reviewer",
    "fixed-topology",
    "adaptive-topology",
    "recon-configured",
    "recon-width-minus-1",
    "recon-same-lens",
    "recon-diverse-lens",
    "research-width-minus-1",
    # A0 = full-pipeline; A1 = recon-risk-swap; A2 = recon-risk-additive.
    "recon-risk-swap",
    "recon-risk-additive",
)
FIXTURE_DIR = Path(__file__).with_name("tasks")
SYNTHETIC_COUNTER = {"provider_calls": 0, "input_tokens": 0, "output_tokens": 0}
MODEL_REGISTRY = {
    "implement-standard": {
        "model": "synthetic-implement",
        "provider": "provider-a",
        "effort": "max",
        "available": True,
    },
    "explore": {
        "model": "synthetic-explore",
        "provider": "provider-b",
        "effort": "high",
        "available": True,
    },
    "explore-thorough": {
        "model": "synthetic-explore-deep",
        "provider": "provider-c",
        "effort": "max",
        "available": True,
    },
    "explore-risk": {
        "model": "minimax-m3",
        "provider": "provider-c",
        "effort": "high",
        "available": True,
    },
    "researcher": {
        "model": "synthetic-research",
        "provider": "provider-a",
        "effort": "max",
        "available": True,
    },
    "researcher-analyst": {
        "model": "synthetic-research-alt",
        "provider": "provider-b",
        "effort": "max",
        "available": True,
    },
    "planner-a": {
        "model": "synthetic-planner-a",
        "provider": "provider-a",
        "effort": "high",
        "available": True,
    },
    "planner-b": {
        "model": "synthetic-planner-b",
        "provider": "provider-b",
        "effort": "high",
        "available": True,
    },
    "planner-c": {
        "model": "synthetic-planner-c",
        "provider": "provider-c",
        "effort": "high",
        "available": True,
    },
    "plan-comparator": {
        "model": "synthetic-plan-comparator",
        "provider": "provider-b",
        "effort": "medium",
        "available": True,
    },
    "judge-primary": {
        "model": "synthetic-judge-a",
        "provider": "provider-a",
        "effort": "medium",
        "available": True,
    },
    "judge-independent": {
        "model": "synthetic-judge-b",
        "provider": "provider-b",
        "effort": "medium",
        "available": True,
    },
    "judge-disagreement": {
        "model": "synthetic-judge-c",
        "provider": "provider-c",
        "effort": "high",
        "available": True,
    },
    "judge-frontier-code": {
        "model": "synthetic-judge-frontier-code",
        "provider": "provider-c",
        "effort": "max",
        "available": True,
    },
    "judge-frontier-general": {
        "model": "synthetic-judge-frontier-general",
        "provider": "provider-a",
        "effort": "max",
        "available": True,
    },
    "verifier-planner": {
        "model": "synthetic-verifier-planner",
        "provider": "provider-b",
        "effort": "high",
        "available": True,
    },
    "frontier-resolver": {
        "model": "synthetic-frontier",
        "provider": "provider-c",
        "effort": "max",
        "available": True,
    },
    "frontier-resolver-standby": {
        "model": "synthetic-frontier-standby",
        "provider": "provider-a",
        "effort": "max",
        "available": True,
    },
    "researcher-challenger": {
        "model": "synthetic-research-challenge",
        "provider": "provider-c",
        "effort": "high",
        "available": True,
    },
    "review-hard": {
        "model": "synthetic-review",
        "provider": "provider-a",
        "effort": "xhigh",
        "available": True,
    },
    "synthetic-review-alt": {
        "model": "synthetic-review-alt",
        "provider": "provider-b",
        "effort": "xhigh",
        "available": True,
    },
    "plan-hard": {
        "model": "synthetic-plan",
        "provider": "provider-c",
        "effort": "xhigh",
        "available": True,
    },
}


def fixture_data(fixture: Any = None) -> dict[str, Any]:
    if fixture is None:
        fixture = "fictional-dashboard-002.json"
    if isinstance(fixture, Mapping):
        return dict(fixture)
    path = Path(fixture)
    if not path.is_file():
        path = FIXTURE_DIR / path
    return json.loads(path.read_text(encoding="utf-8"))


def _profile() -> Profile:
    return Profile("r1-width-three", recon_barrier_width=3, research_barrier_width=3)


def _base(fixture: Any = None) -> tuple:
    task = fixture_data(fixture)
    complexity = task["complexity"]
    return compose_execution(
        "implement",
        complexity,
        "medium",
        "implement-standard",
        None,
        ("pytest -q",),
        True,
        True,
        False,
        _profile(),
    )


def _lens_stage(barrier: str, width: int, repeat: bool = False):
    pool = load_barrier_lenses()[barrier]
    selected = (pool[0],) * width if repeat else pool[:width]
    from grokbuild.decision import ExecutionMember, ExecutionStage

    return ExecutionStage(
        barrier,
        True,
        f"parallel {barrier} reconnaissance barrier",
        (),
        "parallel_spawn",
        "",
        barrier,
        tuple(
            ExecutionMember(f"{barrier}/{i}", role, True, reason)
            for i, (role, reason) in enumerate(selected)
        ),
    )


def _snapshot(
    variant: str, stages: list, overrides: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    bindings = {role: dict(spec) for role, spec in MODEL_REGISTRY.items()}
    for role, binding in (overrides or {}).items():
        bindings[role] = dict(binding)
    emitted = [m.role for stage in stages for m in stage.members] + [
        s.role for s in stages if not s.members and s.kind != "verify"
    ]
    roles = {role: dict(bindings[role]) for role in emitted}
    if any(not bindings.get(role, {}).get("available", False) for role in emitted):
        raise ValueError("emitted role lacks an available synthetic binding")
    return {
        "variant": variant,
        "executable": True,
        "stages": [s.to_dict() for s in stages],
        "roles": roles,
        "provider_counters": dict(SYNTHETIC_COUNTER),
    }


def compose_snapshot(variant: str, fixture: Any = None) -> dict[str, Any]:
    task = fixture_data(fixture)
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    if variant == "adaptive-topology":
        return {
            "variant": variant,
            "executable": False,
            "stages": [],
            "roles": {},
            "provider_counters": dict(SYNTHETIC_COUNTER),
        }
    stages = list(_base(task))
    overrides: dict[str, Mapping[str, Any]] = {}
    if variant in {"single-best-agent", "verifier-only"}:
        stages = [s for s in stages if s.role in {"implement-standard", "verify"}]
    elif variant == "no-recon":
        stages = [s for s in stages if s.stage_id != "recon" and s.role != "explore"]
    elif variant == "no-LLM-review":
        stages = [s for s in stages if not s.role.startswith("review")]
    elif variant == "same-provider-reviewer":
        overrides["review-hard"] = {
            **MODEL_REGISTRY["review-hard"],
            "provider": MODEL_REGISTRY["implement-standard"]["provider"],
        }
    elif variant == "cross-provider-reviewer":
        overrides["review-hard"] = dict(MODEL_REGISTRY["synthetic-review-alt"])
    if variant in {
        "recon-configured",
        "recon-width-minus-1",
        "recon-same-lens",
        "recon-diverse-lens",
        "research-width-minus-1",
        "recon-risk-swap",
        "recon-risk-additive",
    }:
        return compose_r9_snapshot(variant, task)
    return _snapshot(variant, stages, overrides)


def compose_r9_snapshot(row: str, fixture: Any = None) -> dict[str, Any]:
    if row not in {
        "recon-configured",
        "recon-width-minus-1",
        "recon-same-lens",
        "recon-diverse-lens",
        "research-width-minus-1",
        "recon-risk-swap",
        "recon-risk-additive",
    }:
        raise ValueError(row)
    task = fixture_data(fixture)
    stages = list(_base(task))
    if row.startswith("recon-"):
        width = {
            "recon-configured": 3,
            "recon-width-minus-1": 2,
            "recon-same-lens": 3,
            "recon-diverse-lens": 3,
            "recon-risk-swap": 3,
            "recon-risk-additive": 3,
        }[row]
        stages[0] = _lens_stage("recon", width, row == "recon-same-lens")
        if row in {"recon-risk-swap", "recon-risk-additive"}:
            from grokbuild.decision import ExecutionMember

            stage = stages[0]
            members = list(stage.members)
            advisor = ExecutionMember(
                f"recon/{len(members)}",
                "explore-risk",
                True,
                "bounded risk flags with one-minute verification checks",
            )
            if row == "recon-risk-swap":
                advisor = replace(advisor, member_id=members[-1].member_id)
                members[-1] = advisor
            else:
                members.append(advisor)
            stages[0] = replace(stage, members=tuple(members))
    else:
        stages = list(
            compose_execution(
                "research",
                "high",
                "low",
                "researcher",
                None,
                (),
                False,
                False,
                True,
                _profile(),
            )
        )
        stage = next(s for s in stages if s.stage_id == "research")
        pool = load_barrier_lenses()["research"][:2]
        from grokbuild.decision import ExecutionMember

        stages[stages.index(stage)] = replace(
            stage,
            members=tuple(
                ExecutionMember(f"research/{i}", role, True, reason)
                for i, (role, reason) in enumerate(pool)
            ),
        )
    return _snapshot(row, stages)


def account_trace(trace: list[dict[str, str]]) -> dict[str, int]:
    """Count observed synthetic events; each event has stage, member, and state."""
    if not isinstance(trace, list) or any(
        not isinstance(x, dict) or not {"stage", "member", "state"} <= x.keys() for x in trace
    ):
        raise ValueError("malformed synthetic trace")
    planned = len(trace)
    observed = sum(x["state"] == "observed" for x in trace)
    return {"planned": planned, "observed": observed, "missing": planned - observed}
