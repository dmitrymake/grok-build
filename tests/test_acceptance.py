#!/usr/bin/env python3
"""Acceptance tests for the dynamic Grok Build combine router.

Covers the semantic RouteDecision fields, real executor-pipeline selection,
registry capability metadata, explainable scoring/fallback, security
invariants, mode strictness, transactional cache (multiprocess), unspawnable
observe-only behaviour, and spawn-request-vs-result semantics. Stdlib only.
"""

from __future__ import annotations

from _harness import run_hook_main

from _harness import make_check

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"
REPO_CONFIG = REPO_ROOT / "config" / "config.toml"

import json
import multiprocessing as mp
import os
import tempfile


from grokbuild.classify import load_intents  # noqa: E402
from grokbuild.policy import Profile  # noqa: E402
from grokbuild.roles import Role, RoleRegistry, load_registry  # noqa: E402
from grokbuild.router import route_prompt  # noqa: E402
from grokbuild.state import RuntimeState, save_state
from grokbuild.cache import cache_get, cache_put  # noqa: E402
from grokbuild import hook as hook_route  # noqa: E402


def run_main(payload: dict) -> tuple[int, str]:
    return run_hook_main(payload, workspace_root=None)


FAILURES: list[str] = []


check = make_check(FAILURES)


def test_semantic_fields(tmp: Path) -> None:
    spec = load_intents()
    state_path = tmp / "state.json"
    state = RuntimeState(source_path=state_path)
    state.set_available("security-verify", True, "verified test signal")
    save_state(state, state_path)
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        registry=load_registry(REPO_CONFIG),
        mode="dynamic",
        current_model="gpt-5.6-terra",
        session_id="sess-1",
        workspace_root="/tmp/repo",
        state_path=state_path,
        log_path=tmp / "route.jsonl",
        persist=False,
    )
    check(bool(d.decision_id), "decision_id present")
    check(d.policy_id == "default" and d.policy_version == 1, "policy id/version present")
    check(bool(d.repo_id), "repo_id present")
    check(d.turn_id is not None, "turn_id present")
    check(d.version == 4, "schema v4 decision")
    check(d.reasoning_effort == "max", "security reasoning effort is max")
    check(d.task_class == "security", "task_class derived")
    check(d.risk == "high", "risk derived")
    check(d.profile == "default", "profile recorded")
    check(isinstance(d.features, dict) and "word_count" in d.features, "features recorded")
    check(len(d.execution) >= 2, "execution pipeline present")
    sec = [s for s in d.execution if s.role == "security"]
    check(bool(sec) and sec[0].required and sec[0].reason, "executor stage has required/reason")
    check(d.write_policy == "deny", "write_policy recorded")
    check(bool(d.created_at) and bool(d.observed_at), "timestamps recorded")
    check(
        d.candidates and d.candidates[0].role == "security" and d.candidates[0].chosen,
        "candidate selected",
    )


def test_executor_pipeline_selection() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)
    prompt = "прошей роутер на x86 " + " ".join(["слова"] * 90)
    d = route_prompt(
        prompt, spec=spec, mode="static", workspace_root=ws, registry=load_registry(REPO_CONFIG)
    )
    check(d.intent == "implement", "implement intent")
    check(d.complexity == "high" and d.risk == "high", "complexity/risk signals")
    roles = [s.role for s in d.execution]
    check(
        roles == ["recon", "plan-hard", "implement-ops", "review-hard", "verify", "verify"],
        f"executor pipeline {roles}",
    )
    recon = d.execution[0]
    check(
        recon.required
        and recon.kind == "parallel_spawn"
        and [member.role for member in recon.members]
        == ["explore", "explore", "explore-thorough", "explore", "explore"],
        "high implementation uses the default five-member recon barrier",
    )
    check(d.execution[2].required and d.execution[2].role == "implement-ops", "implement required")
    check(
        any("independent review" in w for w in d.warnings),
        "high-risk review degrades without a grok signal",
    )
    verify = [s for s in d.execution if s.kind == "verify"]
    check(
        bool(verify) and verify[0].required and not verify[0].spawnable,
        "deterministic verify stage non-spawnable",
    )

    security_state = RuntimeState()
    security_state.set_available("security-verify", True, "grok login verified (test)")
    d2 = route_prompt(
        "найди RCE в demo-api", spec=spec, mode="static", workspace_root=ws, state=security_state
    )
    check(
        [s.role for s in d2.execution]
        == ["security", "implement-hard", "security-verify", "verify", "verify"],
        "security executor pipeline",
    )

    d3 = route_prompt("спроектируй архитектуру ai-console", spec=spec, mode="static")
    check([s.role for s in d3.execution] == ["plan-hard"], "plan executor pipeline")


def test_classification_expectations() -> None:
    spec = load_intents()
    ws = str(REPO_ROOT)

    d = route_prompt(
        "Добавь unit-тест для существующей функции",
        spec=spec,
        mode="dynamic",
        state=RuntimeState(),
        persist=False,
        registry=load_registry(REPO_CONFIG),
    )
    check(d.intent == "implement", "unit-test prompt is implement")
    check(
        d.role == "implement-cheap" and d.complexity == "low" and d.risk == "low",
        f"unit-test -> cheap Luna ({d.role}/{d.complexity}/{d.risk})",
    )

    d = route_prompt(
        "Сделай repo-wide refactor публичного API с миграцией",
        spec=spec,
        mode="static",
        workspace_root=ws,
        registry=load_registry(REPO_CONFIG),
    )
    check(d.intent == "implement", "repo-wide refactor is implement")
    check(
        d.complexity == "high" and d.risk == "high",
        f"repo-wide is high complexity/risk ({d.complexity}/{d.risk})",
    )
    check(d.role == "implement-hard", f"repo-wide -> implement-hard ({d.role})")
    check(
        [s.role for s in d.execution]
        == ["recon", "plan-hard", "implement-hard", "review-hard", "verify", "verify"],
        f"repo-wide degraded review-hard pipeline ({[s.role for s in d.execution]})",
    )

    state = RuntimeState()
    state.set_available("review-independent", True, "grok login verified (test)")
    d2 = route_prompt(
        "Сделай repo-wide refactor публичного API с миграцией",
        spec=spec,
        mode="dynamic",
        state=state,
        workspace_root=ws,
        persist=False,
        registry=load_registry(REPO_CONFIG),
    )
    check(
        [s.role for s in d2.execution]
        == ["recon", "plan-hard", "implement-hard", "review-panel", "verify", "verify"],
        f"high-risk review uses family-independent reviewer ({[s.role for s in d2.execution]})",
    )

    security_state = RuntimeState()
    security_state.set_available("security-verify", True, "grok login verified (test)")
    d3 = route_prompt(
        "добавь контроль доступа в админку",
        spec=spec,
        mode="static",
        state=security_state,
        workspace_root=ws,
        registry=load_registry(REPO_CONFIG),
    )
    check(d3.intent == "security" and d3.risk == "high", "access control is security/high risk")
    check(
        [s.role for s in d3.execution]
        == ["security", "implement-hard", "security-verify", "verify", "verify"],
        f"security access-control pipeline ({[s.role for s in d3.execution]})",
    )


def test_registry_capabilities() -> None:
    reg = load_registry(REPO_CONFIG)
    sec = reg.get("security")
    check(sec is not None and sec.security is True, "security flag derived")
    check(sec.write is False, "security read-only derived from capability mode")
    check(sec.provider == "zai", f"provider from provider pool ({sec.provider})")
    check(sec.context == 1000000, "context from config")
    check(
        sec.cost == 0.35 and sec.latency == 0.45 and sec.quality_prior == 0.72,
        "tier priors from provider pool",
    )
    rev = reg.get("review")
    check(
        rev is not None and rev.review is True and rev.model == "gpt-5.6-sol",
        "review alias -> review-hard (Sol internal review)",
    )
    check(rev.write is False, "review-hard internal review read-only")
    check(rev.require_availability_signal is False, "review-hard does not need a grok auth signal")
    indep = reg.get("review-independent")
    check(
        indep is not None and indep.review is True and indep.model == "grok-4.6",
        "review-independent distinct (grok-4.6)",
    )
    check(
        indep.require_availability_signal is True,
        "grok review requires explicit availability signal",
    )
    check(reg.get("implement").write is True, "implement can write")
    check(reg.get("implement-cheap-fallback").write is True, "implement-cheap-fallback can write")
    check(reg.get("explore").write is False, "explore is read-only")
    check(reg.get("security-verify").write is False, "security-verify read-only")


def test_explainable_scoring_and_fallback() -> None:
    from grokbuild.pipeline import score_candidates

    reg = RoleRegistry(
        {
            "security": Role(
                name="security",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
                quality_prior=0.8,
                fallback=("security-verify",),
            ),
            "security-verify": Role(
                name="security-verify",
                model="glm-5.3",
                capability_mode="read-only",
                write=False,
                security=True,
                quality_prior=0.6,
            ),
        },
        known_models=frozenset({"glm-5.3"}),
    )
    state = RuntimeState()
    state.record_request("security")
    state.record_completion("security", True)
    state.record_completion("security", True)
    state.set_available("security", False, "down")

    candidates, chosen = score_candidates("security", reg, state, None, security_intent=True)
    check(chosen is not None and chosen.role == "security-verify", f"fallback selected ({chosen})")
    check(all(c.reasons for c in candidates), "candidates carry explainable reasons")


def test_cheap_cannot_remove_security() -> None:
    spec = load_intents()
    state = RuntimeState()
    state.set_available("security-verify", True, "grok login verified (test)")
    d = route_prompt(
        "route=cheap: найди RCE в demo-api",
        spec=spec,
        registry=load_registry(REPO_CONFIG),
        mode="static",
        state=state,
    )
    check(d.intent == "security", "cheap modifier keeps security intent")
    check(d.preference == "cheap", "cheap modifier recorded")
    check(d.role == "security" and d.model == "glm-5.3", "security role/model not downgraded")
    check(any(s.role == "security" and s.required for s in d.execution), "security stage intact")
    check(
        any(s.role == "security-verify" and s.required for s in d.execution),
        "security verification intact",
    )
    check(
        any("independent review" in w for w in d.warnings),
        "high-risk review degrades without a grok signal",
    )


def test_mode_strictness(tmp: Path) -> None:
    spec = load_intents()
    base = tmp / "modes"
    state_path = base / "state.json"
    log_path = base / "route.jsonl"

    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="static",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(not state_path.exists() and not log_path.exists(), "static never writes state/log")
    check(not d.would_deny_edits and d.write_policy == "observe", "static never enforces")

    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        mode="shadow",
        current_model="gpt-5.6-terra",
        state_path=state_path,
        log_path=log_path,
        persist=True,
    )
    check(state_path.exists() and log_path.exists(), "shadow writes state/log")
    check(not d.would_deny_edits and not d.would_block_stop, "shadow never deny/block")

    os.environ["GROK_ROUTE_MODE"] = "shadow"
    os.environ["GROK_ROUTE_ENFORCE"] = "1"
    try:
        d = route_prompt(
            "найди RCE в demo-api",
            spec=spec,
            current_model="gpt-5.6-terra",
            state=RuntimeState(),
            persist=False,
        )
        check(d.mode == "shadow" and not d.would_deny_edits, "GROK_ROUTE_MODE=shadow beats enforce")
    finally:
        os.environ.pop("GROK_ROUTE_MODE", None)
        os.environ.pop("GROK_ROUTE_ENFORCE", None)


def test_cache_transactional_multiprocess(tmp: Path) -> None:
    path = tmp / "route.json"

    def worker(i: int) -> None:
        cache_put(
            {
                "session_id": f"s{i % 3}",
                "turn_id": i,
                "prompt_key": f"k{i}",
                "intent": "security",
                "source": "hook",
            },
            path=path,
        )

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=worker, args=(i,)) for i in range(20)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    data = json.loads(path.read_text(encoding="utf-8"))
    check(data.get("version") == 2, "cache version persisted")
    check(len(data.get("entries", {})) == 20, "all concurrent writes survived")
    rec = cache_get("s1", "k1", 1, path=path)
    check(rec is not None and rec.get("intent") == "security", "per-key cache lookup works")

    cache_put(
        {"session_id": "s9", "turn_id": 9, "prompt_key": "k9", "intent": "security"},
        path=path,
        ttl=0.0,
    )
    check(cache_get("s9", "k9", 9, path=path) is None, "expired cache entry is ignored")


def test_unspawnable_observe_only() -> None:
    spec = load_intents()
    reg = RoleRegistry({})
    d = route_prompt(
        "найди RCE в demo-api",
        spec=spec,
        registry=reg,
        mode="dynamic",
        current_model="gpt-5.6-terra",
        persist=False,
    )
    check(d.intent == "security", "still classifies")
    check(d.role_spawnable is False, "unspawnable role flagged")
    check(d.would_deny_edits and d.would_block_stop, "high-risk exhaustion blocks both gates")
    check(d.write_policy == "deny", "high-risk escalation denies writes")
    check(any("not spawnable" in w for w in d.warnings), "warning recorded")
    check(any("BLOCKED:" in w for w in d.warnings), "escalation warning recorded")


def test_spawn_request_not_success(tmp: Path) -> None:
    def write(records: list[dict]) -> Path:
        p = tmp / f"h{len(records)}.jsonl"
        p.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
        )
        return p

    spawn = {
        "type": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "name": "spawn_subagent",
                "arguments": json.dumps({"subagent_type": "security"}),
            }
        ],
    }
    user = {"type": "user", "content": "найди RCE"}
    done = {
        "type": "tool_result",
        "tool_call_id": "call_1",
        "content": "review complete: found 3 CVEs",
    }

    st = hook_route.turn_station_status(None, "security", history_path=write([user, spawn, done]))
    check(st == {"requested": True, "result": True}, f"result detected {st}")

    st = hook_route.turn_station_status(None, "security", history_path=write([user, spawn]))
    check(st == {"requested": True, "result": False}, f"mere request is not a result {st}")

    st = hook_route.turn_station_status(None, "security", history_path=write([user]))
    check(st == {"requested": False, "result": None}, f"no request {st}")

    route_rec = {
        "intent": "security",
        "required_model": "glm-5.3",
        "current_model": "gpt-5.6-terra",
        "has_strong": True,
        "mode": "dynamic",
        "role_spawnable": True,
        "decision_id": "acceptance-stop",
        "session_id": "01a0063b-a2dd-7bf2-aafd-4a66cf8377cc",
    }
    original_resolve = hook_route.resolve_route
    original_ensure = hook_route._ensure_track
    original_load = hook_route._load_execution
    original_gate = hook_route._enforceable_gate
    original_active = hook_route._gate_active
    hook_route.resolve_route = lambda *args, **kwargs: route_rec
    hook_route._gate_active = lambda *args, **kwargs: True
    hook_route._ensure_track = lambda *args, **kwargs: None
    hook_route._load_execution = lambda *args, **kwargs: type("Track", (), {"stop_blocks": 0})()
    hook_route._enforceable_gate = lambda *args, **kwargs: (False, "pending security")
    try:
        rc, output = run_main(
            {
                "hookEventName": "Stop",
                "sessionId": route_rec["session_id"],
                "reason": "end_turn",
                "backgroundTasks": [{"type": "subagent", "agentType": "security"}],
            }
        )
    finally:
        hook_route.resolve_route = original_resolve
        hook_route._ensure_track = original_ensure
        hook_route._load_execution = original_load
        hook_route._enforceable_gate = original_gate
        hook_route._gate_active = original_active
    check(
        rc == 0 and '"decision": "block"' in output,
        "running/requested background task does not release the gate",
    )


def test_scoring_threshold_and_weights() -> None:
    spec = load_intents()
    high_threshold = {
        "p": Profile(
            name="p",
            mode="dynamic",
            strong_weight=10.0,
            phrase_weight=1.0,
            topic_weight=1.0,
            min_score=100.0,
        )
    }
    d = route_prompt(
        "напиши патч для demo-api",
        spec=spec,
        profiles=high_threshold,
        profile_name="p",
        mode="static",
    )
    check(d.intent is None and d.reason == "below_threshold", f"min_score enforced ({d.reason})")

    weighted = {
        "p2": Profile(
            name="p2", mode="static", strong_weight=10.0, phrase_weight=1.0, topic_weight=1.0
        )
    }
    d2 = route_prompt(
        "найди RCE в demo-api", spec=spec, profiles=weighted, profile_name="p2", mode="static"
    )
    check(
        d2.score == 10.0 and d2.score_breakdown["security"]["score"] == 10.0,
        "weights applied in breakdown",
    )


def test_explore_intent() -> None:
    spec = load_intents()
    prompts = (
        "исследуй структуру репозитория, только чтение без изменений",
        "explore the codebase read-only, no changes",
        "изучи структуру проекта без изменений",
    )
    for prompt in prompts:
        d = route_prompt(prompt, spec=spec, mode="static")
        check(d.intent == "explore", f"explore intent for {prompt!r}")
        check(d.role == "explore" and d.task_class == "research", "explore role/research task")
        check(
            [s.role for s in d.execution] == ["explore"],
            f"explore executor pipeline {[s.role for s in d.execution]}",
        )
        check(d.execution[0].required and d.execution[0].reason, "explore stage required+reason")
        d2 = route_prompt(
            prompt,
            spec=spec,
            mode="dynamic",
            current_model="gpt-5.6-terra",
            state=RuntimeState(),
            persist=False,
        )
        check(
            d2.would_deny_edits and d2.would_block_stop,
            "explore is edit-gated until its child completes",
        )


def test_review_and_plan_intents() -> None:
    spec = load_intents()
    review = route_prompt(
        "review the diff",
        spec=spec,
        mode="dynamic",
        current_model="gpt-5.6-sol",
        state=RuntimeState(),
        persist=False,
        registry=load_registry(REPO_CONFIG),
    )
    check(review.task_class == "review", "direct review task class")
    check(
        review.role == "review-hard" and review.model == "glm-5.3", "direct review role/model"
    )
    check(
        len(review.execution) == 1
        and review.execution[0].role == "review-hard"
        and review.execution[0].required
        and "read-only" in review.execution[0].reason,
        "direct review has exactly one required read-only stage",
    )
    check(
        not any(s.kind == "verify" or s.role == "review-independent" for s in review.execution),
        "direct review has no verifier or independent review",
    )
    check(
        review.write_policy == "deny" and review.would_block_stop, "direct review initially gates"
    )
    check(
        not any("independent review" in w for w in review.warnings),
        "direct review has no grok warning",
    )

    plan = route_prompt(
        "write an ADR", spec=spec, mode="dynamic", state=RuntimeState(), persist=False
    )
    check([s.role for s in plan.execution] == ["plan-hard"], "ADR pipeline is exactly plan-hard")
    check(plan.would_deny_edits and plan.would_block_stop, "ADR write initially gates on plan-hard")

    no_review = RoleRegistry({})
    unavailable = route_prompt(
        "review the diff", spec=spec, registry=no_review, mode="dynamic", persist=False
    )
    check(
        not unavailable.role_spawnable and unavailable.write_policy == "observe",
        "unavailable review role degrades observe-only",
    )


def test_hook_no_prompt_persistence(tmp: Path) -> None:
    old_state = os.environ.get("XDG_STATE_HOME")
    old_cache = os.environ.get("XDG_CACHE_HOME")
    old_mode = os.environ.get("GROK_ROUTE_MODE")
    os.environ["XDG_STATE_HOME"] = str(tmp / "no-prompt-state")
    os.environ["XDG_CACHE_HOME"] = str(tmp / "no-prompt-cache")
    os.environ["GROK_ROUTE_MODE"] = "shadow"
    try:
        record = hook_route.resolve_route(
            None, load_intents(), persist=True, event="user_prompt_submit"
        )
        log = Path(os.environ["XDG_STATE_HOME"]) / "grok-route" / "route.jsonl"
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        check(
            record.get("reason") == "no_prompt" and record.get("decision_id"),
            "hook no-prompt decision is typed",
        )
        check(
            len(lines) == 1
            and lines[0].get("version") == 4
            and lines[0].get("telemetry_generation") == 1,
            "hook no-prompt submit persists one schema-v4 outcome",
        )
    finally:
        if old_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = old_state
        if old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old_cache
        if old_mode is None:
            os.environ.pop("GROK_ROUTE_MODE", None)
        else:
            os.environ["GROK_ROUTE_MODE"] = old_mode


def test_visual_intake_acceptance(tmp: Path) -> None:
    import hashlib
    from grokbuild.visual_intake import (
        AttachmentRef,
        VisualIntakePolicy,
        VisualIntakeRequestV1,
        run_visual_intake,
    )

    class FakeInvoker:
        def invoke(self, **_kwargs):
            return {
                "schema_version": 1,
                "role_version": "1",
                "attachment_ids": ["img"],
                "image_type": "photo",
                "summary": "A photo.",
                "visible_text": "",
                "key_elements": [],
                "observations": [],
                "likely_relevance": [],
                "uncertainties": [],
                "needs_deeper_visual_analysis": False,
                "confidence": 1.0,
            }

    old_sample_d = os.environ.get("MINIMAX_API_KEY")
    os.environ["MINIMAX_API_KEY"] = "test"
    try:
        request = VisualIntakeRequestV1(
            "describe",
            (AttachmentRef("img", hashlib.sha256(b"image").hexdigest(), "image/png"),),
            VisualIntakePolicy(),
        )
        handoff = run_visual_intake(
            request,
            registry=load_registry(REPO_CONFIG),
            invoker=FakeInvoker(),
            cache_path=tmp / "visual.json",
            prefer_deep=False,
        )
        check(
            handoff.result is not None and handoff.conductor_received_visual_context,
            "visual intake fake-invoker acceptance",
        )
        decision = route_prompt(
            "plain text question", mode="static", registry=load_registry(REPO_CONFIG)
        )
        execution_roles = [stage.role for stage in decision.execution]
        check(
            "visual-intake" not in execution_roles and "visual-intake-deep" not in execution_roles,
            "visual roles are absent from text execution pipeline",
        )
    finally:
        if old_sample_d is None:
            os.environ.pop("MINIMAX_API_KEY", None)
        else:
            os.environ["MINIMAX_API_KEY"] = old_sample_d


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_semantic_fields(tmp)
        test_executor_pipeline_selection()
        test_classification_expectations()
        test_registry_capabilities()
        test_explainable_scoring_and_fallback()
        test_cheap_cannot_remove_security()
        test_explore_intent()
        test_review_and_plan_intents()
        test_mode_strictness(tmp)
        test_hook_no_prompt_persistence(tmp)
        test_cache_transactional_multiprocess(tmp)
        test_unspawnable_observe_only()
        test_spawn_request_not_success(tmp)
        test_scoring_threshold_and_weights()
        test_visual_intake_acceptance(tmp)

    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("acceptance tests passed")
    return 0


if __name__ == "__main__":
    run_standalone(main)
