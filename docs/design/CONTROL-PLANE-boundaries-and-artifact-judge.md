# Control-plane boundaries + artifact-only judge (design / governance)

Status: governance rule (binding for ALL future features) + artifact-judge feature
(north-star arc, not implemented). Origin: Sol architectural review (2026-08-31) +
operator's question "am I reinventing the harness / does this conflict with core?".

## Governance rule (binding): one runtime, many policies

**Answer to "am I reinventing the harness?": No — you are extending the policy and
verification layers over the SAME execution runtime. That is the normal path, on one
condition: no new feature may grow its own execution lifecycle.**

Grounding in the actual code — grok-build already has exactly ONE canonical machine:
- one stage type: `ExecutionStage.kind ∈ {spawn, parallel_spawn, verify, sentinel}`
  (grokbuild/decision.py:59);
- one execution state machine: `ExecutionTrack` — `requested`/`completed`/`failed` for
  spawnable stages, `verified` separately for deterministic verification
  (grokbuild/state.py:202-217);
- one composition point: `compose_execution` (grokbuild/pipeline.py:828);
- one enforcement path: the hook gate.

**THE RULE:** every new capability (judge, verifier-planner, selector, portfolio,
planner-market, failover, cheap-advisors, frontier escalation) expresses itself as
**stages (a `kind`) + policy over this one `ExecutionTrack`**, never as a second task
graph, state machine, lifecycle, tool loop, session state, or retry loop.

A feature that acquires its own `spawn_agent` / `run_tools` / `retry` / `manage_context`
/ `choose_model` has become a second harness. Forbidden.

## The layering (product vs infrastructure)

```
Task / User Interface
        ↑
Multi-model CONTROL PLANE   ← the product (this project)
        ↑
Agent RUNTIME               ← reusable infra (grok-build hook, or OpenCode, ...)
```

- CONTROL PLANE (the product): routing, portfolio construction, decomposition-confidence,
  model-disagreement, verification, artifact-judging, selection, escalation policy,
  budget allocation, model-reliability history, oracle-gap learning.
- RUNTIME (infra): sessions, tool calls, context, MCP, subagents, process execution,
  permissions, providers, streaming.

**Portability test for any new feature:** "if the underlying harness swaps from grok-build
to OpenCode, should this component conceptually survive?" — judge / router / selector /
portfolio / escalation / reliability-db = YES (product); tool-loop / session / MCP /
terminal = NO (infra). Product-side features are what we build; infra is reused. This is
the neutral-ABI / adapter-split direction both earlier architecture reviews named (R8).

## Three separate functions (do NOT merge)

- **ROUTER** — "who should work?" (policy layer).
- **VERIFIER-PLANNER** — "what evidence must we obtain?" → returns a `NeedEvidence`
  request (hypothesis, kind, constraints). It does NOT execute the experiment. Lives in
  policy → produces new task-graph nodes.
- **JUDGE** — "what follows from the evidence at hand?" A near-powerless PURE FUNCTION:
  `verdict = judge.compare(task_contract, artifact_a, artifact_b, evidence)`. It never
  spawns, runs tools, chooses models, decomposes, or holds trajectory state.

Correction to the original judge spec (per Sol): the discriminating-test GENERATION
belongs to the verifier-planner / harness, NOT the judge. The judge only says
"cannot distinguish; need evidence of kind X"; the HARNESS decides how to obtain it (which
test, who runs it, cost, safety) by composing a new `kind=evidence_collection` stage, then
re-running the judge stage. Keeping the judge dumb makes it reliable — it needs no
orchestration state.

The harness remains the subject; judge/verifier/router are functions.

## Artifact-only multi-model judge (feature)

Purpose: select the best of multiple candidate results while minimizing model-identity,
style, verbosity, self-preference, position, and fabrication bias. **Core invariant:
"judges select artifacts, never agents."**

Placement in grok-build: VERIFICATION layer, on top of R2 typed evidence (the "canonical
artifact bundle" IS typed evidence — R2 is the foundation; the judge is the selection
layer). It is a new stage `kind` (e.g. `judge`) over the one `ExecutionTrack`; the gate
runs it like any spawned stage; `NEEDS_DISCRIMINATING_TEST` → harness composes an
`evidence_collection` stage → re-run judge stage.

Key mechanics (full spec kept by operator; essentials):
- Judge NEVER sees: model/provider/agent name, raw assistant response, reasoning/CoT,
  candidate's own plan/explanation, tool trajectory, identity-bearing history, commit
  author, another judge's verdict. Judge sees ONLY: canonical task spec, anonymous
  candidate artifacts, deterministic/independent verification evidence, baseline.
- Pipeline: extract artifacts → identity-sanitize → canonical bundle → HARD verifiers
  first (deterministic; a hard-requirement failure dominates any subjective advantage;
  lexicographic precedence, not weighted average) → PAIRWISE judging (never "score 1-10";
  ask "is A demonstrably preferable to B?"), two judges with REVERSED candidate order,
  independent. Selector v2 supersedes the plain hard→pairwise chain with artifact clustering
  and a consensus prior before pairwise judging. Agreement + evidence → select. Disagreement → discriminating test (harness)
  → new evidence → re-judge → still unresolved/high-impact → frontier adjudicator (same
  sanitized artifact-only format; no prior judges' identities/rationales).
- Strict structured verdict JSON (verdict/confidence/requirement_results/hard_failures/
  reason_codes(enum)/decisive_evidence_refs/discriminating_test); evidence-pointing, not
  long prose. Missing evidence = UNKNOWN, never guessed. Fail-closed if identity leaks
  (identity-leak tests + position-bias tests are deliverables). UNJUDGEABLE if a valid
  bundle can't be built — never silently fall back to raw model responses.
- Roles (model-agnostic config bindings, roles-primary): `judge.primary`,
  `judge.independent`, `judge.disagreement`, `judge.frontier_code`, `judge.frontier_general`.
  Operator's default: Luna(A,B) + Gemini(B,A) as the two cheap judges; buy Grok/Sol/Opus
  only after a discriminating test fails to resolve.
- Metrics goal: maximize `selector_capture = selector_gain / oracle_gain`
  (selector_gain = selector_success − best_single_success; oracle_gain =
  portfolio_oracle_success − best_single_success), NOT judge Elo in isolation. Persist an
  anonymized judge-calibration dataset (task spec, bundles, evidence, decisions, accepted
  candidate, later regressions, human corrections) — never use provider identity as a
  quality feature.

## Sequencing (unchanged priority: live tool works now)

This is a FEATURE arc of the control plane, dependent on R2 (typed evidence). Order stays:
incident-cluster bugs → FAILOVER Slice-2 → cheap-advisors → planner-market → frontier
escalation → **artifact-judge + selector** (this doc) → learned router. Nothing here is
urgent; bugs first. Each increment is R1-ablation-measured; measured against the
matched-budget Harness Report ("system vs one smart trajectory").

## Non-goals / cautions
- No second task graph / state machine / lifecycle (the governance rule).
- Judge is a pure function; verifier-planner and router are separate; harness is subject.
- No majority-voting overriding a deterministic hard failure.
- Roles-primary: judge model bindings are config; swap without touching orchestration.
- Build on R2 typed evidence, don't fork a parallel evidence format.
