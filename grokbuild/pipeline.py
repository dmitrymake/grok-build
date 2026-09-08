#!/usr/bin/env python3
"""Multi-stage routing + executor-selection pipeline for Grok Build combine.

The pipeline runs deterministic stages, then selects an *executor pipeline* of
spawnable roles (plus a non-spawnable deterministic verification stage) from
the actual task class, complexity, risk, clarity/acceptance/testability,
failure history, availability and quota pressure.

Implementation work uses the primary Codex tiers (cheap/standard/hard), the
specialized ops role, then overflow. `explore` is read-only Luna recon and is
never an implementer. `implement-cheap-fallback` is the final writable Flash
fallback, only for a cheap job under strict low-risk, explicit-acceptance, and
known-verifier conditions. On failure/unavailability the primary ladder
escalates cheap -> standard -> hard before cross-provider fallback.

Stages: extract → modifier → override → score → select → role → availability →
executor tier/fallback → executor compose → policy gate → record.

`Mode` bounds what the pipeline may do:
- `static`: pure classification, no state/log/cache and never enforces.
- `shadow`: classify + persist state/log, but never deny/block.
- `dynamic`: classify + persist + compute deny/block, always respecting the
  effective profile (unspawnable roles force observe-only, never a gate).

The hook remains responsible for the final deny/block decision because it knows
what the pipeline cannot (spawn completion this turn, and the real Stop reason).
"""

from __future__ import annotations

import os
import json
import string
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from typing import Any, Mapping

from grokbuild.classify import load_intents, model_allowed
from grokbuild.decision import (
    SCHEMA_VERSION,
    Candidate,
    ExecutionStage,
    RouteDecision,
    _route_decision_base,
    decision_to_dict,
    hash_id,
)
from grokbuild.endpoint_resolution import (
    make_endpoint_resolution,
    persist_endpoint_resolution,
)
from grokbuild.evidence import (
    EvidenceRecord,
    FailureSignal,
    classify_failure,
    make_evidence,
    persist_evidence,
)
from grokbuild.features import extract_features
from grokbuild.policy import (
    POLICY_VERSION,
    Profile,
    gated_intents,
    is_truthy_env,
    load_profiles,
    pick_winner,
    resolve_mode_from_env,
    score_intents,
)
import grokbuild.roles as roles
from grokbuild.roles import (
    RoleRegistry,
    credential_present,
    load_registry,
    role_credential_present,
)
from grokbuild.availability import (
    _endpoint_cache,  # noqa: F401
    _model_endpoint_availability,  # noqa: F401
    _quota_acceptable,  # noqa: F401
    _refresh_stale_quota,  # noqa: F401
    _role_availability,  # noqa: F401
    _role_available,  # noqa: F401
    _role_failed,  # noqa: F401
    _role_pressured,  # noqa: F401
    QUOTA_SIGNAL_TTL,  # noqa: F401
    REFRESH_MIN_INTERVAL,  # noqa: F401  # noqa: F401
)
from grokbuild.verifiers import (
    VERIFIER_SENTINEL,  # noqa: F401
    apply_config_verifiers,  # noqa: F401
    is_verifier_command,  # noqa: F401
    load_verifier_map,  # noqa: F401
    parse_verifier_argv,  # noqa: F401
    resolve_verifier,  # noqa: F401
)
from grokbuild.state import (
    RuntimeState,
    current_turn_tx,
    default_log_path,
    default_state_path,
    load_state,
)
from grokbuild.transactions import allocate_turn, record_decision_tx
from grokbuild.persist import append_jsonl


from grokbuild.compose import (
    _soft_review_independence,
    compose_consilium_barrier,
    compose_execution,
    has_required_execution,
    load_barrier_lenses,
    validate_execution,
    compose_frontier_stage,
    compose_judge_panel,
    compose_judge_rereads,
    compose_judge_challengers,
    compose_evidence_stage,
    compose_adjudication_stage,
)


def _second_opinion_config() -> tuple[str, str]:
    try:
        path = roles.resolve_config_path()
        if path is None:
            raise ValueError
        table = (
            tomllib.loads(path.read_text(encoding="utf-8"))
            .get("routing", {})
            .get("second_opinion", {})
        )
        if not isinstance(table, dict):
            raise ValueError
        model, provider = (
            table.get("model", "gemini-3.7-flash"),
            table.get("provider", "commandcode"),
        )
        if (
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(provider, str)
            or not provider.strip()
        ):
            raise ValueError
        return model, provider
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError):
        return "gemini-3.7-flash", "commandcode"


TASK_BY_INTENT = {
    "security": "security",
    "implement": "coding",
    "review": "review",
    "plan": "planning",
    "explore": "research",
    "research": "research",
}
_MARKERS_FALLBACK = {
    "destructive": ("проши", "прошей", "прошить", "sysupgrade", "прошивк"),
    "destructive_verbs": (),
    "destructive_targets": (),
    "risk_terms": (
        "authentication",
        "authorization",
        "authz",
        "authn",
        "password",
        "credential",
        "crypto",
        "token",
        "session fixation",
        "jwt",
        "oauth",
        "permission",
    ),
    "medium_complexity": ("medium-complexity", "medium complexity"),
    "high_complexity": (
        "repo-wide",
        "repo wide",
        "public api",
        "public-api",
        "public_api",
        "publicapi",
        "публичн",
        "migration",
        "migrate",
        "миграц",
        "мигрир",
        "concurrency",
        "race condition",
        "race-condition",
        "data race",
        "конкурентн",
        "параллелизм",
        "многопоточн",
        "потокобезопасн",
        "гонк",
    ),
    "testable": (
        "test",
        "тест",
        "acceptance",
        "spec",
        "регресси",
        "regression",
        "unit-test",
        "unit test",
        "unit-тест",
        "unit тест",
        "юнит",
        "тестирован",
        "testing",
    ),
}


def _load_markers():
    try:
        data = json.loads(
            (Path(__file__).resolve().parent / "intents.json").read_text(encoding="utf-8")
        ).get("markers", {})
        if isinstance(data, dict) and all(
            isinstance(data.get(k), list) and all(isinstance(x, str) for x in data[k])
            for k in _MARKERS_FALLBACK
        ):
            return {k: tuple(data[k]) for k in _MARKERS_FALLBACK}
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return _MARKERS_FALLBACK


_MARKERS = _load_markers()
DESTRUCTIVE_MARKERS = _MARKERS["destructive"]
DESTRUCTIVE_VERB_MARKERS = _MARKERS["destructive_verbs"]
DESTRUCTIVE_TARGET_MARKERS = _MARKERS["destructive_targets"]
RISK_TERMS = _MARKERS["risk_terms"]
MEDIUM_COMPLEXITY_MARKERS = _MARKERS["medium_complexity"]
HIGH_COMPLEXITY_IMPLEMENT_MARKERS = _MARKERS["high_complexity"]


IMPLEMENT_TIERS = ("implement-cheap", "implement-standard", "implement-strong", "implement-hard")
# Explicit acceptance/testability signals only. A bare short prompt is NOT a
# testable prompt: the writable Flash fallback must require concrete
# test/acceptance/spec markers (and a known verifier), never a word-count proxy.
TESTABLE_MARKERS = _MARKERS["testable"]
READ_ONLY_STAGES = roles.EXECUTION_READ_ONLY_ROLES
IMPLEMENT_STAGES = roles.IMPLEMENT_ROLE_NAMES
SECURITY_STAGES = frozenset({"security", "security-verify"})
REVIEW_STAGES = frozenset({"review", "review-hard", "review-independent", "expert-rescue"})
# Independent review roles must be family-distinct from the implementer.
# Internal review may run same-family only as an explicit degraded mode.
INDEPENDENT_REVIEW_ROLES = frozenset({"review-independent", "expert-rescue"})
INTERNAL_REVIEW_ROLES = frozenset({"review", "review-hard"})
DEGRADED_REVIEW_REASON_CODE = "degraded_review_same_family"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _enforce_on(env: Mapping[str, str], enforce_default: bool) -> bool:
    if env.get("GROK_ROUTE_ENFORCE") is None:
        return bool(enforce_default)
    return is_truthy_env(env, "GROK_ROUTE_ENFORCE")


def compute_complexity(features: Any, scored: Mapping[str, Any]) -> str:
    words = int(features.word_count)
    n_intents = len(scored)
    norm = features.normalized
    if (
        any(marker in norm for marker in HIGH_COMPLEXITY_IMPLEMENT_MARKERS)
        or words >= 80
        or n_intents >= 2
    ):
        return "high"
    if any(marker in norm for marker in MEDIUM_COMPLEXITY_MARKERS) or words >= 30:
        return "medium"
    return "low"


# The data noun matches by exact token only: the stem "данн" would also hit
# "данный"/"данную" (an adjective, not a target).
_DATA_NOUN_TARGETS = frozenset({"данные", "данных", "данным", "данными"})

# Trailing/leading punctuation stays glued to whitespace tokens ("удали,
# пожалуйста") and would defeat both the exact data-noun set and the
# база-данных adjacency; quotes and the ellipsis are not in string.punctuation.
_TOKEN_TRIM = string.punctuation + "«»„“”…"


def _compound_destructive(norm: str) -> bool:
    """True when a destructive-verb token sits within 3 tokens of a target token.

    Whitespace tokens trimmed of edge punctuation; stems match by prefix
    (Russian inflection). Targets: single-word stems, the exact data-noun
    tokens above, and the adjacent "баз данных" pair (token[i] startswith
    "баз" AND token[i+1] startswith "данн", counted at i+1) — the bare "баз"
    prefix alone never counts, so "базовый"/"базированный" stay
    non-destructive.
    """
    tokens = [token.strip(_TOKEN_TRIM) for token in (norm or "").split()]
    tokens = [token for token in tokens if token]
    verbs = tuple(v for v in DESTRUCTIVE_VERB_MARKERS if " " not in v)
    stems = tuple(t for t in DESTRUCTIVE_TARGET_MARKERS if " " not in t)
    verb_at = [
        index for index, token in enumerate(tokens) if any(token.startswith(v) for v in verbs)
    ]
    if not verb_at:
        return False

    def target_at(index: int) -> bool:
        token = tokens[index]
        if token in _DATA_NOUN_TARGETS:
            return True
        if any(token.startswith(stem) for stem in stems):
            return True
        return index > 0 and tokens[index - 1].startswith("баз") and token.startswith("данн")

    for index in range(len(tokens)):
        if target_at(index) and any(0 < abs(index - verb) <= 3 for verb in verb_at):
            return True
    return False


def _is_destructive(norm: str) -> bool:
    """Plain destructive marker hit or a destructive verb/target compound."""
    return any(marker in norm for marker in DESTRUCTIVE_MARKERS) or _compound_destructive(norm)


def compute_risk(intent: str | None, features: Any) -> str:
    norm = features.normalized
    if intent == "security":
        return "high"
    if any(term in norm for term in RISK_TERMS):
        return "high"
    if intent == "implement":
        if _is_destructive(norm):
            return "high"
        if any(marker in norm for marker in HIGH_COMPLEXITY_IMPLEMENT_MARKERS):
            return "high"
        if _explicit_testable(features):
            return "low"
        return "medium"
    if intent == "plan":
        return "low"
    return "low"


def _cause_label(signal: FailureSignal) -> str:
    cause = classify_failure(signal)
    return {
        "auth": "auth-class",
        "environment": "infrastructure",
        "model": "model-class",
        "unknown": "unknown-class",
    }[cause]


def _tagged_failure(signal: FailureSignal) -> str:
    return f"{_cause_label(signal)}: {signal.detail() or 'unavailable'}"


def _explicit_testable(features: Any) -> bool:
    if features is None:
        return False
    norm = getattr(features, "normalized", "") or ""
    return any(marker in norm for marker in TESTABLE_MARKERS)


def _risk_ceiling_allows(role_name: str, risk: str, registry: RoleRegistry) -> bool:
    role = registry.get(role_name)
    ceiling = role.risk_ceiling if role else None
    rank = {"low": 0, "medium": 1, "high": 2}
    return ceiling is None or rank.get(risk, 2) <= rank.get(ceiling, -1)


def _cheap_fallback_allowed(risk: str, features: Any, verifier_known: bool) -> bool:
    """True only when the writable Flash fallback may replace a cheap job.

    The writable Flash fallback is strictly constrained: low risk, explicit
    acceptance/test markers, and a known deterministic verifier for the
    workspace. Anything else escalates to Terra instead.
    """
    return risk == "low" and _explicit_testable(features) and bool(verifier_known)


def select_implement_role(
    complexity: str,
    risk: str,
    features: Any,
    state: RuntimeState | None,
    registry: RoleRegistry,
    preference: str | None,
    stale: bool = False,
    verifier_known: bool = False,
    session_id: str | None = None,
    decision_id: str | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
    endpoint_events: list[dict[str, Any]] | None = None,
) -> tuple[str | None, list[str], bool]:
    """Choose a writable implementation role with cause-tagged degradation evidence."""

    reasons: list[str] = []
    scope = int(getattr(features, "word_count", 0) or 0) if features else 0
    norm = getattr(features, "normalized", "") or ""
    destructive = _is_destructive(norm)

    failures: dict[str, FailureSignal] = {}

    def usable(name: str) -> bool:
        available, signal = _role_availability(
            state, name, registry, stale, session_id, endpoint_events
        )
        if not available:
            failures[name] = signal or FailureSignal(reason="unavailable")
            return False
        role = registry.get(name)
        if _role_pressured(state, name):
            failures[name] = FailureSignal(
                reason="quota pressured", provider=role.provider if role else ""
            )
            return False
        if _role_failed(state, name, session_id):
            status = (
                state.session_status_for(session_id, name)
                if state is not None and session_id
                else (state.status_for(name) if state is not None else None)
            )
            failures[name] = FailureSignal(
                reason=(status.reason if status else "") or "role failed",
                provider=role.provider if role else "",
            )
            return False
        return True

    def skipped(name: str) -> str:
        signal = failures.get(name, FailureSignal(reason="unavailable/pressured/failed"))
        if decision_id and evidence_records is not None:
            evidence_records.append(
                make_evidence(
                    decision_id=decision_id,
                    session_id=session_id,
                    subject={"path": "implement-selection", "role": name},
                    signal=signal,
                )
            )
        return f"skipped {name} (unavailable/pressured/failed)"

    if destructive:
        order = [
            "implement-ops",
            "implement-hard",
            "implement-strong",
            "implement-standard",
            "implement-cheap",
        ]
        desired = "implement-ops"
        reasons.append("tier=ops (firmware/destructive operations)")
    elif preference == "quality" or complexity == "high" or risk == "high":
        order = ["implement-hard", "implement-strong", "implement-standard", "implement-cheap"]
        desired = "implement-hard"
        reasons.append("tier=hard (high complexity/risk or quality preference)")
    elif (
        complexity == "low"
        and risk == "low"
        and (preference == "cheap" or _explicit_testable(features) or scope <= 60)
    ):
        order = ["implement-cheap", "implement-standard", "implement-strong", "implement-hard"]
        desired = "implement-cheap"
        reasons.append(
            "tier=cheap (non-high complexity, low risk, and cheap/testable/scope preference)"
        )
    else:
        order = ["implement-standard", "implement-hard", "implement-cheap"]
        desired = "implement-standard"
        reasons.append("tier=standard (default)")

    for name in order:
        if not _risk_ceiling_allows(name, risk, registry):
            reasons.append(f"skipped {name} (risk ceiling exceeded)")
            continue
        if usable(name):
            degraded = name != desired
            if degraded:
                reasons.append(f"fallback to {name} ({desired} unavailable/pressured/failed)")
            return name, reasons, degraded
        reasons.append(skipped(name))

    # Overflow workhorse after every primary writable tier failed.
    # The model pin lives in config.toml, not in the role name.
    if not _risk_ceiling_allows("implement-overflow", risk, registry):
        reasons.append("skipped implement-overflow (risk ceiling exceeded)")
    elif usable("implement-overflow"):
        reasons.append("overflow after primary writable tiers failed")
        return "implement-overflow", reasons, True
    else:
        reasons.append(skipped("implement-overflow"))

    if _cheap_fallback_allowed(risk, features, verifier_known) and usable(
        "implement-cheap-fallback"
    ):
        reasons.append(
            "last-resort writable Flash fallback for low-risk testable work with a known verifier"
        )
        return "implement-cheap-fallback", reasons, True

    reasons.append(f"no available implement tier (desired {desired})")
    return None, reasons, True


def review_independent_available(
    state: RuntimeState | None,
    registry: RoleRegistry,
    stale: bool,
    session_id: str | None = None,
) -> bool:
    """True when the independent host-session review role is verified available.

    The host-session role requires an explicit safe runtime availability signal
    because its authorization is not independently verified in this harness.
    """
    return _role_available(state, "review-independent", registry, stale, session_id)


def score_candidates(
    primary: str | None,
    registry: RoleRegistry,
    state: RuntimeState,
    preference: str | None,
    security_intent: bool,
    stale: bool = False,
    session_id: str | None = None,
) -> tuple[list[Candidate], Candidate | None]:
    """Choose the best *available* spawnable role, with an explainable score.

    Stale state contributes neutral runtime signals: availability and circuit
    are treated as healthy and quota/observed stats are ignored.
    """

    primary = registry.canonical(primary)
    primary_role = registry.get(primary)
    names: list[str] = []
    if primary_role:
        names.append(primary_role.name)
    if primary_role:
        for fallback in primary_role.fallback:
            role = registry.get(fallback)
            if role and role.name not in names:
                # A security task must never fall back to a non-security role.
                if security_intent and not role.security:
                    continue
                names.append(role.name)

    candidates: list[Candidate] = []
    for name in names:
        role = registry.get(name)
        if role is None:
            continue
        if not stale and not _role_available(state, name, registry, stale, session_id):
            continue
        status = state.status_for(name)
        reasons: list[str] = []
        score = role.quality_prior if role.quality_prior is not None else 0.5
        reasons.append(f"prior={score:.2f}")
        if not stale:
            rate = state.success_rate(name)
            if rate is not None:
                score += 0.15 * (rate - 0.5)
                reasons.append(f"success_rate={rate:.2f}")
            pressure = status.quota_pressure()
            if pressure > 0:
                score -= 0.3 * pressure
                reasons.append(f"quota_pressure={pressure:.2f}")
        if preference == "cheap":
            cost = role.cost if role.cost is not None else 0.5
            score += 0.2 * (1.0 - cost)
            reasons.append("pref=cheap")
        elif preference == "quality":
            q = role.quality_prior if role.quality_prior is not None else 0.5
            score += 0.2 * q
            reasons.append("pref=quality")
        elif preference == "fast":
            latency = role.latency if role.latency is not None else 0.5
            score += 0.2 * (1.0 - latency)
            reasons.append("pref=fast")
        if role.preference is not None:
            score += 0.01 * role.preference
            reasons.append(f"preference={role.preference}")
        candidates.append(Candidate(role=name, score=round(score, 6), reasons=tuple(reasons)))

    if not candidates:
        return [], None
    chosen = max(candidates, key=lambda c: c.score)
    return candidates, chosen


def decision_outcome(decision: RouteDecision) -> dict[str, Any]:
    """Compact, redaction-safe outcome for the JSONL log and state history."""

    base = _route_decision_base(decision)
    return {
        "version": base["version"],
        "telemetry_generation": base["telemetry_generation"],
        "decision_id": base["decision_id"],
        "policy_id": base["policy_id"],
        "repo_id": base["repo_id"],
        "turn_id": base["turn_id"],
        "prompt_key": base["prompt_key"],
        "observed_at": base["observed_at"],
        "created_at": base["created_at"],
        "event": base["event"],
        "session_id": base["session_id"],
        "intent": base["intent"],
        "task_class": base["task_class"],
        "complexity": base["complexity"],
        "risk": base["risk"],
        "role": base["role"],
        "model": base["model"],
        "reasoning_effort": base["reasoning_effort"],
        "current_model": base["current_model"],
        "allowed": base["allowed"],
        "enforce": base["enforce"],
        "would_deny_edits": base["would_deny_edits"],
        "would_block_stop": base["would_block_stop"],
        "write_policy": base["write_policy"],
        "mode": base["mode"],
        "profile": base["profile"],
        "source": base["source"],
        "reason": base["reason"],
        "score": base["score"],
        "confidence": base["confidence"],
        "second_opinion": base["second_opinion"],
        "role_spawnable": base["role_spawnable"],
        "execution_valid": base["execution_valid"],
        "role_available": base["role_available"],
        "circuit_open": base["circuit_open"],
        "execution": base["execution"],
        "candidates": base["candidates"],
        "fallbacks": base["fallbacks"],
        "warnings": base["warnings"],
        "stage_trace": base["stage_trace"],
    }


@dataclass
class _RouteContext:
    prompt: str
    session_id: str | None
    current_model: str | None
    event: str
    source: str
    persist: bool
    workspace_root: str | None
    turn_id: int | None
    observed_at: str
    trace: list[tuple[str, str]]
    base: dict[str, Any]
    features: Any = None
    modifier: str | None = None
    override_intent: str | None = None
    scored: Mapping[str, Any] = None
    winner: str | None = None
    below_threshold: bool = False
    decision: RouteDecision | None = None
    prompt_key: str | None = None
    decision_id: str | None = None
    repo_id: str | None = None
    intent: Mapping[str, Any] = None
    role_name: str = ""
    task_class: str = "general"
    complexity: str = "low"
    risk: str = "low"
    warnings: list[str] = None
    stale: bool = False
    verify_commands: tuple[str, ...] | None = None
    review_independent_ok: bool = False
    researcher_challenger_ok: bool = False
    explore_risk_ok: bool = False
    review_hard_ok: bool = False
    security_verifier_ok: bool = False
    selected_role: str | None = None
    impl_role: str | None = None
    candidates: list[Candidate] = None
    role_available: bool | None = None
    circuit_open: bool = False
    quota_used: int | None = None
    role_spawnable: bool = False
    model: str | None = None
    reasoning_effort: str | None = None
    how: Any = None
    block_tools: tuple[str, ...] = ()
    strong: Any = ()
    has_strong: bool = False
    reason: str = ""
    score: float = 0.0
    confidence: float = 0.0
    second_opinion: Any = None
    evidence_records: list[EvidenceRecord] = None
    endpoint_events: list[dict[str, Any]] = None
    execution: tuple[ExecutionStage, ...] = ()
    execution_valid: bool = True
    allowed: bool = True
    gated_intent: bool = False
    enforce: bool = False
    would_deny_edits: bool = False
    would_block_stop: bool = False
    escalation_required: bool = False
    write_policy: str = "observe"
    score_breakdown: dict[str, Any] = None
    features_summary: dict[str, Any] = None


@dataclass
class Pipeline:
    spec: Mapping[str, Any] | None = None
    registry: RoleRegistry | None = None
    profiles: Mapping[str, Profile] | None = None
    profile_name: str | None = None
    mode: str | None = None
    state: RuntimeState | None = None
    state_path: Any = None
    log_path: Any = None
    enforce_default: bool = False

    def __post_init__(self) -> None:
        self.spec = self.spec if self.spec is not None else load_intents()
        self.spec = apply_config_verifiers(self.spec)
        self.profiles = self.profiles if self.profiles is not None else load_profiles()
        chosen = self.profile_name or os.environ.get("GROK_ROUTE_PROFILE") or "default"
        self.profile_name = chosen
        self.profile = self.profiles.get(chosen, self.profiles.get("default"))
        assert self.profile is not None
        self.registry = self.registry if self.registry is not None else load_registry()
        self.mode = self.mode or resolve_mode_from_env(os.environ, self.profile)
        if self.mode == "static":
            self.state = self.state if self.state is not None else RuntimeState()
        else:
            self.state = self.state if self.state is not None else load_state(self.state_path)
        self.state_path = (
            self.state_path
            if self.state_path is not None
            else (self.state.source_path or default_state_path())
        )
        self.log_path = self.log_path if self.log_path is not None else default_log_path()
        self.executable_bindings: dict[str, dict[str, str | None]] = {}

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        current_model: str | None = None,
        event: str = "resolve",
        source: str = "none",
        persist: bool = False,
        workspace_root: str | None = None,
        turn_id: int | None = None,
    ) -> RouteDecision:
        """Run the routing stages in their historical order."""
        observed_at = _now()
        prompt = prompt or ""
        ctx = _RouteContext(
            prompt=prompt,
            session_id=session_id,
            current_model=current_model,
            event=event,
            source=source,
            persist=persist,
            workspace_root=workspace_root,
            turn_id=turn_id,
            observed_at=observed_at,
            trace=[],
            evidence_records=[],
            endpoint_events=[],
            base=dict(
                session_id=session_id,
                current_model=current_model,
                event=event,
                source=source,
                mode=self.mode,
                observed_at=observed_at,
                created_at=observed_at,
            ),
        )
        if not prompt.strip():
            ctx.decision = RouteDecision(
                version=SCHEMA_VERSION,
                decision_id=hash_id(
                    "decision", self.profile_name, session_id or "", "empty", observed_at
                ),
                policy_id=self.profile_name,
                policy_version=POLICY_VERSION,
                repo_id=hash_id("repo", workspace_root) if workspace_root else None,
                turn_id=current_turn_tx(self.state_path, session_id)
                if self.mode != "static"
                else None,
                prompt_key=hash_id("prompt", session_id or "", "empty"),
                reason="no_prompt",
                profile=self.profile_name,
                prompt_chars=0,
                write_policy="observe",
                stage_trace=tuple(ctx.trace),
                **ctx.base,
            )
            return self._finalize(ctx)
        self._extract(ctx)
        self._score(ctx)
        if self._select(ctx):
            return self._finalize(ctx)
        self._compose(ctx)
        self._gate(ctx)
        return self._finalize(ctx)

    def _extract(self, ctx: _RouteContext) -> None:
        """Extract features and append the extract, modifier, and override labels."""
        ctx.features = extract_features(ctx.prompt, self.spec)
        ctx.trace.append(
            (
                "extract",
                "override"
                if ctx.features.override
                else ("modifier" if ctx.features.modifier else "ok"),
            )
        )
        ctx.modifier = ctx.features.modifier
        ctx.trace.append(("modifier", ctx.modifier or "none"))
        if ctx.features.override:
            if self.profile.allow_override:
                ctx.override_intent = ctx.features.override
                ctx.trace.append(("override", ctx.override_intent))
            else:
                ctx.trace.append(("override", "disabled"))
        else:
            ctx.trace.append(("override", "none"))

    def _score(self, ctx: _RouteContext) -> None:
        """Score intents and append the score trace label."""
        ctx.scored = score_intents(ctx.features, self.spec, self.profile)
        order = tuple(self.profile.priority) or tuple(self.spec.get("priority") or ())
        ctx.winner = ctx.override_intent or pick_winner(ctx.scored, order)
        if (
            ctx.winner is not None
            and not ctx.override_intent
            and ctx.scored.get(ctx.winner) is not None
            and ctx.scored[ctx.winner].score < self.profile.min_score
        ):
            ctx.winner = None
            ctx.below_threshold = True
        ctx.trace.append(
            (
                "score",
                ",".join(f"{name}={item.score:g}" for name, item in sorted(ctx.scored.items()))
                or "none",
            )
        )

    def _select(self, ctx: _RouteContext) -> bool:
        """Select a role and append the select, tier, and availability trace labels."""
        ctx.trace.append(
            ("select", ctx.winner or ("below_threshold" if ctx.below_threshold else "none"))
        )
        if self.mode == "static":
            ctx.turn_id = None
        elif ctx.turn_id is not None:
            ctx.turn_id = int(ctx.turn_id)
        elif ctx.persist:
            prompt_fingerprint = hash_id(
                "prompt-fingerprint", ctx.session_id or "", ctx.features.normalized
            )
            ctx.turn_id = allocate_turn(self.state_path, ctx.session_id, prompt_fingerprint)
        else:
            ctx.turn_id = current_turn_tx(self.state_path, ctx.session_id)
        ctx.prompt_key = hash_id(
            "prompt", ctx.session_id or "", str(ctx.turn_id or 0), ctx.features.normalized
        )
        ctx.decision_id = hash_id(
            "decision", str(SCHEMA_VERSION), self.profile_name, ctx.session_id or "", ctx.prompt_key
        )
        ctx.repo_id = hash_id("repo", ctx.workspace_root) if ctx.workspace_root else None
        if ctx.winner is None:
            # Deliberate observe-only policy: an unmatched prompt is never
            # guessed into a role. intent stays None (reason no_intent or
            # below_threshold), execution is empty, write_policy observe, no
            # gates, and no second opinion.
            ctx.decision = RouteDecision(
                version=SCHEMA_VERSION,
                decision_id=ctx.decision_id,
                policy_id=self.profile_name,
                policy_version=POLICY_VERSION,
                repo_id=ctx.repo_id,
                turn_id=ctx.turn_id,
                prompt_key=ctx.prompt_key,
                task_class="general",
                complexity=compute_complexity(ctx.features, ctx.scored),
                risk="low",
                reason="below_threshold" if ctx.below_threshold else "no_intent",
                score=0.0,
                confidence=0.0,
                profile=self.profile_name,
                override=ctx.override_intent,
                preference=ctx.modifier,
                write_policy="observe",
                features={
                    "word_count": ctx.features.word_count,
                    "length": ctx.features.length,
                    "override": ctx.features.override,
                    "modifier": ctx.features.modifier,
                },
                prompt_chars=ctx.features.length,
                stage_trace=tuple(ctx.trace),
                **ctx.base,
            )
            return True
        intents = self.spec.get("intents", {}) if isinstance(self.spec, Mapping) else {}
        intent = intents.get(ctx.winner) or {}
        ctx.intent = intent
        ctx.role_name = self.registry.canonical(str(intent.get("role") or ctx.winner)) or str(
            intent.get("role") or ctx.winner
        )
        primary_role = self.registry.get(ctx.role_name)
        ctx.task_class = TASK_BY_INTENT.get(ctx.winner, "general")
        ctx.complexity = compute_complexity(ctx.features, ctx.scored)
        ctx.risk = compute_risk(ctx.winner, ctx.features)
        ctx.warnings = []
        ctx.stale = self.mode != "static" and self.state.stale
        ctx.verify_commands = resolve_verifier(ctx.workspace_root, self.spec)
        ctx.review_independent_ok = review_independent_available(
            self.state, self.registry, ctx.stale, ctx.session_id
        )
        ctx.researcher_challenger_ok = _role_available(
            self.state, "researcher-challenger", self.registry, ctx.stale, ctx.session_id
        )
        ctx.explore_risk_ok = False
        advisor_role = self.profile.recon_advisor_role
        if advisor_role and ctx.winner == "implement" and ctx.complexity in {"medium", "high"}:
            advisor = self.registry.get(advisor_role)
            advisor_meta = (
                self.registry.provider_catalog.get(advisor.model or "") if advisor else None
            )
            ctx.explore_risk_ok = (
                bool(advisor)
                and role_credential_present(advisor, advisor_meta, os.environ)
                and _role_available(
                    self.state, advisor_role, self.registry, ctx.stale, ctx.session_id
                )
            )
            if not ctx.explore_risk_ok:
                ctx.warnings.append(
                    "recon advisor unavailable (binding, credential, or provider); degraded by omission"
                )
                ctx.trace.append(("availability", "explore_risk_unavailable"))
        ctx.review_hard_ok = _role_available(
            self.state, "review-hard", self.registry, ctx.stale, ctx.session_id
        )
        ctx.security_verifier_ok = self.mode == "static" or _role_available(
            self.state, "security-verify", self.registry, ctx.stale, ctx.session_id
        )
        ctx.selected_role = ctx.role_name
        ctx.impl_role = None
        ctx.candidates = []
        ctx.role_available = None
        ctx.circuit_open = False
        ctx.quota_used = None
        if ctx.task_class == "coding":
            tier_stale = self.mode == "static" or ctx.stale
            ctx.selected_role, tier_reasons, degraded = select_implement_role(
                ctx.complexity,
                ctx.risk,
                ctx.features,
                self.state,
                self.registry,
                ctx.modifier,
                stale=tier_stale,
                verifier_known=ctx.verify_commands is not None,
                session_id=ctx.session_id,
                decision_id=ctx.decision_id,
                evidence_records=ctx.evidence_records,
                endpoint_events=ctx.endpoint_events,
            )
            for reason in tier_reasons:
                ctx.trace.append(("tier", reason))
            ctx.candidates = (
                [
                    Candidate(
                        role=ctx.selected_role, score=1.0, reasons=tuple(tier_reasons), chosen=True
                    )
                ]
                if ctx.selected_role
                else []
            )
            if degraded:
                ctx.warnings.append(f"degraded fallback selected for '{ctx.role_name}'")
            if ctx.selected_role is None:
                ctx.role_spawnable = False
                ctx.role_available = False
                ctx.circuit_open = (
                    self.state.circuit_open(ctx.role_name, session_id=ctx.session_id)
                    if self.mode != "static"
                    else False
                )
                if ctx.risk == "high":
                    ctx.escalation_required = True
                    ctx.warnings.append(
                        "BLOCKED: no capable+available model of class high; quota/human needed."
                    )
                else:
                    ctx.warnings.append(
                        f"no available implement tier for '{ctx.role_name}'; observe-only (no gate)"
                    )
                ctx.trace.append(("availability", "unavailable"))
            else:
                ctx.role_spawnable = True
                if self.mode != "static":
                    ctx.role_available = True
                    ctx.circuit_open = self.state.circuit_open(
                        ctx.selected_role, session_id=ctx.session_id
                    )
                    ctx.quota_used = self.state.status_for(ctx.selected_role).quota_used
                if ctx.selected_role != ctx.role_name:
                    ctx.warnings.append(
                        f"tier selected: '{ctx.role_name}' -> '{ctx.selected_role}'"
                    )
                ctx.trace.append(("availability", "ok" if self.mode != "static" else "unknown"))
            ctx.impl_role = ctx.selected_role
        else:
            ctx.role_spawnable = primary_role is not None
            if self.mode == "static" or not ctx.role_spawnable:
                if not ctx.role_spawnable:
                    ctx.warnings.append(
                        f"role '{ctx.role_name}' is not spawnable; observe-only (no gate)"
                    )
                    ctx.trace.append(("availability", "unspawnable"))
                else:
                    ctx.trace.append(("availability", "unknown"))
            else:
                ctx.candidates, chosen = score_candidates(
                    ctx.role_name,
                    self.registry,
                    self.state,
                    ctx.modifier,
                    security_intent=(ctx.task_class == "security"),
                    stale=ctx.stale,
                    session_id=ctx.session_id,
                )
                if chosen is None:
                    _, signal = _role_availability(
                        self.state,
                        ctx.role_name,
                        self.registry,
                        ctx.stale,
                        ctx.session_id,
                    )
                    if ctx.decision_id and signal is not None:
                        ctx.evidence_records.append(
                            make_evidence(
                                decision_id=ctx.decision_id,
                                session_id=ctx.session_id,
                                subject={"path": "role-selection", "role": ctx.role_name},
                                signal=signal,
                            )
                        )
                    ctx.warnings.append(
                        f"role '{ctx.role_name}' unavailable or circuit-open; observe-only (no gate)"
                    )
                    ctx.role_spawnable = False
                    ctx.role_available = False
                    ctx.circuit_open = self.state.circuit_open(
                        ctx.role_name, session_id=ctx.session_id
                    )
                    ctx.trace.append(("availability", "unavailable"))
                else:
                    ctx.selected_role = chosen.role
                    ctx.candidates = [
                        Candidate(
                            role=c.role,
                            score=c.score,
                            reasons=c.reasons,
                            chosen=(c.role == ctx.selected_role),
                        )
                        for c in ctx.candidates
                    ]
                    ctx.role_available = True
                    ctx.circuit_open = self.state.circuit_open(
                        ctx.selected_role, session_id=ctx.session_id
                    )
                    ctx.quota_used = self.state.status_for(ctx.selected_role).quota_used
                    if ctx.selected_role != ctx.role_name:
                        ctx.warnings.append(
                            f"fallback selected: '{ctx.role_name}' -> '{ctx.selected_role}'"
                        )
                    ctx.trace.append(("availability", "ok"))
        if ctx.winner == "security" and ctx.task_class == "security":
            sec_impl, sec_reasons, _ = select_implement_role(
                ctx.complexity,
                "high",
                ctx.features,
                self.state,
                self.registry,
                ctx.modifier,
                stale=self.mode == "static" or ctx.stale,
                verifier_known=ctx.verify_commands is not None,
                session_id=ctx.session_id,
                decision_id=ctx.decision_id,
                evidence_records=ctx.evidence_records,
                endpoint_events=ctx.endpoint_events,
            )
            for reason in sec_reasons:
                ctx.trace.append(("tier", reason))
            if sec_impl is None:
                ctx.role_spawnable = False
                ctx.role_available = False
                ctx.impl_role = None
                ctx.escalation_required = True
                ctx.warnings.append(
                    "BLOCKED: no capable+available model of class high; quota/human needed."
                )
                ctx.trace.append(("availability", "unavailable"))
            else:
                ctx.impl_role = sec_impl
            if not ctx.security_verifier_ok:
                ctx.role_spawnable = False
                ctx.role_available = False
                ctx.warnings.append(
                    "security verifier unavailable (no safe availability signal); whole security route is observe-only and must not be described as verified"
                )
                ctx.trace.append(("availability", "security_verifier_unavailable"))
        if (
            ctx.winner in {"security", "implement", "plan"}
            and ctx.risk == "high"
            and not ctx.review_independent_ok
        ):
            if ctx.winner == "implement" and ctx.review_hard_ok:
                ctx.warnings.append(
                    "independent review (host session) unavailable (no safe availability signal); degraded to internal review-hard"
                )
            else:
                ctx.warnings.append(
                    "independent review (host session) unavailable (no safe availability signal); degraded observe-only"
                )
        if (
            ctx.winner == "research"
            and ctx.complexity == "high"
            and not ctx.researcher_challenger_ok
        ):
            ctx.warnings.append(
                "research challenger (host session) unavailable (no safe availability signal); degraded to two-member research barrier"
            )
            ctx.trace.append(("availability", "researcher_challenger_unavailable"))
        return False

    def _compose(self, ctx: _RouteContext) -> None:
        """Compose execution before the execution and policy trace labels."""
        selected_role_obj = self.registry.get(ctx.selected_role)
        ctx.model = (
            selected_role_obj.model if selected_role_obj else (ctx.intent.get("model") or None)
        )
        ctx.reasoning_effort = selected_role_obj.reasoning_effort if selected_role_obj else None
        ctx.how = ctx.intent.get("how")
        ctx.block_tools = tuple(str(x) for x in (ctx.intent.get("block_tools") or []))
        picked = ctx.scored.get(ctx.winner)
        ctx.strong = picked.strong if picked else ()
        ctx.has_strong = bool(ctx.strong) or ctx.override_intent is not None
        ctx.reason = (
            "security_verifier_unavailable"
            if ctx.winner == "security" and not ctx.security_verifier_ok
            else ("override" if ctx.override_intent else "matched")
        )
        ctx.score = picked.score if picked else self.profile.min_score
        ctx.confidence = 1.0 if ctx.override_intent else round(min(1.0, ctx.score / 10.0), 6)
        selected_meta = self.registry.provider_catalog.get(ctx.model or "")
        selected_provider = getattr(selected_meta, "provider", "unknown")
        opinion_model, opinion_provider = _second_opinion_config()
        # Calibrated threshold 0.3: one strong needle (score 3.0 -> 0.3) already
        # settles the route, so only phrase-only matches (score 1.0-2.0 ->
        # confidence 0.1-0.2) buy a second opinion.
        if ctx.confidence < 0.3 and selected_provider != opinion_provider:
            opinion_meta = self.registry.provider_catalog.get(opinion_model)
            credential_ok = credential_present(opinion_meta, os.environ)
            provider_ok = self.state.is_provider_available(opinion_provider)
            eligible = credential_ok and provider_ok
            reason = "low-confidence-routing"
            if not eligible:
                provider_status = self.state.provider_availability.get(opinion_provider)
                signal = FailureSignal(
                    reason=(
                        "credential missing"
                        if not credential_ok
                        else (provider_status.reason if provider_status else "")
                        or "provider unavailable"
                    ),
                    provider=opinion_provider,
                )
                reason = "provider-unavailable"
                ctx.evidence_records.append(
                    make_evidence(
                        decision_id=ctx.decision_id,
                        session_id=ctx.session_id,
                        subject={"path": "second-opinion", "model": opinion_model},
                        signal=signal,
                    )
                )
            ctx.second_opinion = {
                "model": opinion_model if eligible else None,
                "provider": opinion_provider,
                "reason": reason,
                "confidence": float(ctx.confidence),
            }
        ctx.execution = (
            compose_execution(
                ctx.winner,
                ctx.complexity,
                ctx.risk,
                ctx.selected_role,
                ctx.impl_role,
                ctx.verify_commands,
                review_independent_ok=ctx.review_independent_ok,
                review_hard_ok=ctx.review_hard_ok,
                researcher_challenger_ok=ctx.researcher_challenger_ok,
                profile=self.profile,
                warnings=ctx.warnings,
                explore_risk_ok=ctx.explore_risk_ok,
            )
            if ctx.role_spawnable
            else ()
        )
        if (
            ctx.winner == "implement"
            and ctx.role_spawnable
            and ctx.execution
            and (ctx.risk == "high" or ctx.complexity in {"medium", "high"})
        ):
            review_composed = any(
                stage.role in REVIEW_STAGES
                or any(member.role in REVIEW_STAGES for member in stage.members)
                for stage in ctx.execution
            )
            if not review_composed:
                if not ctx.verify_commands:
                    ctx.role_spawnable = False
                    ctx.role_available = False
                    ctx.execution = ()
                    ctx.warnings.append(
                        "review unavailable and no configured deterministic verifier; observe-only (no gate)"
                    )
                    ctx.trace.append(("execution", "review_unavailable"))
                else:
                    ctx.warnings.append("review unavailable; degraded to deterministic verify only")

    def _gate(self, ctx: _RouteContext) -> None:
        """Validate execution and append the execution and policy trace labels."""
        ctx.execution_valid = True
        if ctx.role_spawnable and ctx.execution:
            ctx.execution, review_warnings = _soft_review_independence(ctx.execution, self.registry)
            ctx.warnings.extend(review_warnings)
            ctx.execution_valid, stage_warnings = validate_execution(ctx.execution, self.registry)
            ctx.warnings.extend(
                warning for warning in stage_warnings if warning not in ctx.warnings
            )
            if not ctx.execution_valid:
                ctx.trace.append(("execution", "invalid"))
        else:
            ctx.trace.append(("execution", "none" if not ctx.execution else "ok"))
        ctx.allowed = model_allowed(ctx.current_model, ctx.model) if ctx.model else True
        ctx.gated_intent = ctx.winner in gated_intents(self.spec)
        ctx.enforce = _enforce_on(os.environ, self.enforce_default)
        gate_strong = (
            ctx.has_strong
            or ctx.winner in self.profile.gate_phrase_intents
            or not self.profile.require_strong_for_gate
        )
        initial_gate = (
            self.mode == "dynamic"
            and ctx.role_spawnable
            and ctx.execution_valid
            and ctx.gated_intent
            and has_required_execution(ctx.execution)
            and gate_strong
        )
        escalation_gate = self.mode == "dynamic" and ctx.escalation_required and ctx.gated_intent
        ctx.would_deny_edits = initial_gate or escalation_gate
        ctx.would_block_stop = initial_gate or escalation_gate
        ctx.write_policy = "observe"
        if escalation_gate:
            ctx.allowed = False
            ctx.write_policy = "deny"
        elif self.mode == "dynamic" and ctx.role_spawnable and ctx.execution_valid:
            ctx.write_policy = "deny" if ctx.would_deny_edits else "allow"
        ctx.trace.append(
            ("policy", "gate" if (ctx.would_deny_edits or ctx.would_block_stop) else "pass")
        )
        ctx.score_breakdown = {
            name: {
                "strong": len(item.strong),
                "phrases": len(item.phrases),
                "topics": len(item.topics),
                "score": item.score,
            }
            for name, item in sorted(ctx.scored.items())
        }
        ctx.features_summary = {
            "word_count": ctx.features.word_count,
            "length": ctx.features.length,
            "override": ctx.features.override,
            "modifier": ctx.features.modifier,
            "strong_count": sum(1 for _, v in ctx.features.strong for _ in v),
            "phrase_count": sum(1 for _, v in ctx.features.phrases for _ in v),
        }
        ctx.decision = RouteDecision(
            version=SCHEMA_VERSION,
            decision_id=ctx.decision_id,
            policy_id=self.profile_name,
            policy_version=POLICY_VERSION,
            repo_id=ctx.repo_id,
            turn_id=ctx.turn_id,
            prompt_key=ctx.prompt_key,
            intent=ctx.winner,
            task_class=ctx.task_class,
            complexity=ctx.complexity,
            risk=ctx.risk,
            role=ctx.selected_role if ctx.role_spawnable else ctx.role_name,
            model=ctx.model,
            reasoning_effort=ctx.reasoning_effort,
            how=ctx.how,
            block_tools=ctx.block_tools,
            matches=tuple(
                (name, item.strong + item.phrases) for name, item in sorted(ctx.scored.items())
            ),
            strong=ctx.strong,
            has_strong=ctx.has_strong,
            reason=ctx.reason,
            score=ctx.score,
            confidence=ctx.confidence,
            second_opinion=ctx.second_opinion,
            execution=ctx.execution,
            candidates=tuple(ctx.candidates),
            fallbacks=tuple(c.role for c in ctx.candidates if not c.chosen),
            score_breakdown=ctx.score_breakdown,
            features=ctx.features_summary,
            profile=self.profile_name,
            override=ctx.override_intent,
            preference=ctx.modifier,
            write_policy=ctx.write_policy,
            role_spawnable=ctx.role_spawnable,
            execution_valid=ctx.execution_valid,
            role_available=ctx.role_available,
            circuit_open=ctx.circuit_open,
            role_quota_used=ctx.quota_used,
            allowed=ctx.allowed,
            enforce=ctx.enforce,
            would_deny_edits=ctx.would_deny_edits,
            would_block_stop=ctx.would_block_stop,
            warnings=tuple(ctx.warnings),
            prompt_chars=ctx.features.length,
            stage_trace=tuple(ctx.trace),
            **ctx.base,
        )

    def _finalize(self, ctx: _RouteContext) -> RouteDecision:
        """Persist the decision and return it."""
        self.executable_bindings = {
            str(event["role"]): dict(event["executable_binding"])
            for event in ctx.endpoint_events or ()
            if event.get("role") and isinstance(event.get("executable_binding"), Mapping)
        }
        if ctx.persist and self.mode != "static":
            record_decision_tx(
                self.state_path,
                decision_to_dict(ctx.decision),
                ctx.selected_role if ctx.role_spawnable else None,
            )
            outcome = decision_outcome(ctx.decision)
            outcome["provider_availability"] = self.state.provider_availability_snapshot()
            append_jsonl(self.log_path, outcome)
            for event in ctx.endpoint_events or ():
                persist_endpoint_resolution(
                    make_endpoint_resolution(
                        decision_id=ctx.decision_id,
                        session_id=ctx.session_id,
                        event=event,
                        observed_at=ctx.observed_at,
                    )
                )
                for skipped in event.get("skipped", ()):
                    ctx.evidence_records.append(
                        make_evidence(
                            decision_id=ctx.decision_id,
                            session_id=ctx.session_id,
                            subject={
                                "path": "endpoint-resolution",
                                "model": str(event.get("model") or ""),
                                "endpoint": str(skipped.get("endpoint") or ""),
                            },
                            signal=FailureSignal(
                                reason=str(skipped.get("reason") or "endpoint unavailable"),
                                provider=str(skipped.get("provider") or ""),
                            ),
                        )
                    )
            for record in ctx.evidence_records or ():
                persist_evidence(record)
        return ctx.decision


__all__ = [
    "Pipeline",
    "decision_outcome",
    "score_candidates",
    "select_implement_role",
    "review_independent_available",
    "compose_execution",
    "load_barrier_lenses",
    "compute_complexity",
    "compute_risk",
    "validate_execution",
    "compose_consilium_barrier",
    "compose_frontier_stage",
    "compose_judge_panel",
    "compose_judge_rereads",
    "compose_judge_challengers",
    "compose_evidence_stage",
    "compose_adjudication_stage",
    "resolve_verifier",
    "is_verifier_command",
    "parse_verifier_argv",
    "default_state_path",
    "default_log_path",
]
