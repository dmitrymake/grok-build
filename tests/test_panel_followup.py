from __future__ import annotations

import json

from grokbuild import compose, hook, roles, settlement
from grokbuild.policy import Profile
from grokbuild.roles import ProviderModelMeta, Role
from grokbuild.state import default_state_path
from grokbuild.transactions import ensure_execution_tx


def _registry():
    base = roles.load_registry()
    role_map = dict(base._roles)
    catalog = dict(base.provider_catalog)
    specs = {
        "implement-hard": ("panel-impl", "impl-family", True, False),
        "review-hard": ("panel-review", "review-family", False, True),
        "review-independent": ("panel-independent", "independent-family", False, True),
        "expert-rescue": ("panel-rescue", "rescue-family", False, True),
    }
    for name, (model, family, write, review) in specs.items():
        old = role_map[name]
        role_map[name] = Role(
            **{
                **old.__dict__,
                "model": model,
                "write": write,
                "capability_mode": "all",
                "review": review,
            }
        )
        catalog[model] = ProviderModelMeta(
            provider=f"provider-{family}",
            provider_label=family,
            tier="test",
            cost=0,
            latency=0,
            quality_prior=1,
            family=family,
        )
    return roles.RoleRegistry(role_map, provider_catalog=catalog)


def test_three_family_review_panel_composes_all_members():
    registry = _registry()
    warnings: list[str] = []
    stages = compose.compose_execution(
        "implement",
        "high",
        "low",
        "implement-hard",
        "implement-hard",
        review_hard_ok=True,
        review_independent_ok=True,
        expert_rescue_ok=True,
        profile=Profile("test", review_barrier_width=3),
        registry=registry,
        warnings=warnings,
    )
    panels = [stage for stage in stages if stage.stage_id == "review-panel"]
    assert len(panels) == 1
    panel = panels[0]
    assert panel.kind == "parallel_spawn" and panel.slot == "review"
    assert [member.member_id for member in panel.members] == ["0", "1", "2"]
    assert len(panel.members) == 3
    families = [
        roles.family_of(registry.get(member.role).model, registry.provider_catalog)
        for member in panel.members
    ]
    assert len(set(families)) == 3
    assert families[0] != roles.family_of(
        registry.get("implement-hard").model, registry.provider_catalog
    )


def test_review_panel_hook_reports_wrong_parallel_member(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = _registry()
    panel = next(
        stage
        for stage in compose.compose_execution(
            "implement",
            "high",
            "low",
            "implement-hard",
            "implement-hard",
            review_hard_ok=True,
            review_independent_ok=True,
            expert_rescue_ok=True,
            profile=Profile("test", review_barrier_width=3),
            registry=registry,
        )
        if stage.stage_id == "review-panel"
    )
    decision_id = "panel-decision"
    ensure_execution_tx(
        default_state_path(),
        decision_id,
        "panel-session",
        1,
        [stage.to_dict() for stage in [panel]],
    )
    monkeypatch.setattr(hook.roles, "load_registry", lambda: registry)
    monkeypatch.setattr(hook, "_canonical_role", lambda value: value)
    monkeypatch.setattr(settlement, "_canonical_role", lambda value: value)
    monkeypatch.setattr(hook, "_gate_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(hook, "_enforceable_gate", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr(hook, "_next_required_spawn_stage", lambda track: panel)
    allocations = iter(("review-panel/0", "review-panel/1", "review-panel/2", None))
    monkeypatch.setattr(
        hook, "allocate_parallel_member_tx", lambda *args, **kwargs: next(allocations)
    )
    monkeypatch.setattr(
        hook,
        "resolve_route",
        lambda *args, **kwargs: {
            "decision_id": decision_id,
            "mode": "dynamic",
            "role_spawnable": True,
            "would_deny_edits": True,
            "gate_active": True,
        },
    )
    records: list[dict[str, object]] = []
    monkeypatch.setattr(hook, "_append_log", lambda payload, state=None: records.append(payload))
    outcomes = []
    for role in ("review-hard", "review-independent", "expert-rescue", "review-hard"):
        try:
            hook.handle_pre_tool(
                {
                    "sessionId": "panel-session",
                    "toolName": "spawn_subagent",
                    "toolInput": {"subagent_type": role},
                },
                {},
            )
        except SystemExit:
            pass
        outcomes.append(json.loads(capsys.readouterr().out))
    assert [item["reason_code"] for item in records[:3]] == [
        "parallel_member_allowed",
        "parallel_member_allowed",
        "parallel_member_allowed",
    ]
    assert [item["member"] for item in records[:3]] == [
        "review-panel/0",
        "review-panel/1",
        "review-panel/2",
    ]
    assert outcomes[3]["decision"] == "deny"
    assert records[3]["reason_code"] == "wrong_parallel_member"
    assert all(item["reason_code"] != "wrong_stage" for item in records)


def test_lens_file_without_optional_review_keeps_other_pools(tmp_path):
    source = json.loads((compose._BARRIER_LENSES_PATH).read_text())
    source["barriers"].pop("review")
    path = tmp_path / "lenses.json"
    path.write_text(json.dumps(source))
    loaded = compose.load_barrier_lenses(path)
    assert loaded["recon"] == tuple((x["role"], x["reason"]) for x in source["barriers"]["recon"])
    assert "review" not in loaded
    assert compose._FALLBACK_BARRIER_LENSES["review"]


def test_real_catalog_width_three_degrades_with_warning():
    warnings: list[str] = []
    stages = compose.compose_execution(
        "implement",
        "high",
        "low",
        "implement-hard",
        "implement-hard",
        review_hard_ok=True,
        review_independent_ok=True,
        expert_rescue_ok=True,
        profile=Profile("test", review_barrier_width=3),
        warnings=warnings,
    )
    assert any(stage.stage_id == "review-panel" for stage in stages)
    assert not any("review_barrier_width clamped" in warning for warning in warnings)


def test_default_high_complexity_review_is_linear():
    stages = compose.compose_execution(
        "implement",
        "high",
        "low",
        "implement-standard",
        "implement-standard",
        review_hard_ok=True,
        profile=Profile("default"),
    )
    review = [stage for stage in stages if stage.role == "review-hard"]
    assert len(review) == 1 and review[0].kind == "spawn"


def test_judge_rereads_are_bounded_and_blind():
    stages = compose.compose_judge_rereads(
        Profile("judge", artifact_judge=True, judge_extra_reads_max=3)
    )
    assert len(stages) == 1
    assert len(stages[0].members) == 3
    assert [member.reason for member in stages[0].members] == [
        "compare A then B",
        "compare B then A",
        "compare A then B",
    ]


def test_disabled_challengers_never_enter_a_panel_but_opt_in_bound_roles_do():
    registry = roles.load_registry()
    assert compose.compose_judge_challengers(Profile("default"), registry=registry) == []
    stages = compose.compose_judge_challengers(
        Profile("eval", judge_challengers=True), registry=registry
    )
    assert len(stages) == 1
    assert [member.role for member in stages[0].members] == [
        "judge-challenger-agentic",
        "judge-challenger-structural",
    ]
    assert registry.get("judge-challenger-agentic").model == "gpt-oss-120b"
    assert registry.get("judge-challenger-structural").model == "gpt-oss-120b"
    assert registry.get("judge-challenger-structural").reasoning_effort == "low"
