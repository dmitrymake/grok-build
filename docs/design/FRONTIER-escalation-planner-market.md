# Frontier escalation via planner-market (design / roadmap)

Status: north-star architecture. Not implemented. Built incrementally, each step
measured (R1 ablation), AFTER the incident-cluster bug fixes. Origin: Sol
architectural review (2026-08-30) + operator directives.

## Guiding principle: roles are primary, models are replaceable

Every element below is a ROLE. Model bindings live in config and are swappable.
When a stronger model ships (MiniMax M3-Pro on 2026-09-03; OpenAI Astra when/if it
releases and proves out), it slots into an existing role without touching the
architecture. We never block the design on an unreleased model.

## The core idea (Sol)

Do NOT trust a single model's self-reported confidence (`confidence=0.74`). Instead
detect uncertainty from OBSERVABLE signals, and spend expensive intelligence only on
the intellectually dense part of a task, not on boilerplate.

**Planner market.** N cheap, diverse planners independently produce a task
decomposition. High agreement → decomposition is obvious → cheap swarm executes.
High disagreement → buy expensive intelligence (frontier) for framing / decomposition
/ invariants / verification strategy. Disagreement across independent models is a far
stronger signal than one model's confidence score.

```
TASK → cheap task-understanding
        ├── decomposition confidence HIGH → cheap swarm execution
        └── decomposition confidence LOW  → FRONTIER (framing, decomposition,
                                            invariants, unknowns, verification plan)
                                            → cheap swarm executes the plan
```

**Frontier is a meta-planner, not an executor.** The most expensive model does the
work with the highest marginal value of intelligence (~10k tokens: understand,
architecture, decomposition, invariants, verification strategy) and hands a plan to
cheap models that do the ~200k tokens of implement/test/search/inspect/refactor/review.
Frontier is a rare resolver — on ~100 engineering tasks, roughly: 55 trivial (one cheap
model), 25 decomposable (cheap swarm), 12 difficult-but-recognizable (stronger
specialist), 8 genuinely ambiguous (frontier) — and even those 8, frontier only frames,
it does not execute.

**Escalation is not last resort.** Escalating AFTER a bad cheap plan already ran a
swarm wastes compute and pollutes the trajectory. The market/score gates BEFORE
execution.

## Roles (model-agnostic bindings)

- `planner-a` / `planner-b` / `planner-c` — cheap, provider-diverse independent
  decomposition. Candidate bindings: DeepSeek@opencode, DeepSeek@commandcode, GLM-5.3,
  MiniMax (M3 / M3-Pro). Provider diversity is the point — independent world-models.
- `plan-comparator` — computes agreement/disagreement over the planners' task graphs.
  This is a SIGNAL producer, never a vote-as-truth arbiter.
- `frontier-resolver` — the rare meta-planner. Binding NOW: `grok-4.6` (strongest
  available reasoning). Later candidates: MiniMax M3-Pro (2026-09-03), Astra (if it
  ships and proves out). Role is permanent; model swaps via config.
- Existing execution/review/security/verify roles consume the plan and do the work.

## `frontier_score` — observable escalation signals (Sol)

Not one model's confidence, but a weighted set of observable signals:

| Signal | Meaning |
| --- | --- |
| planner disagreement | the decomposition itself is unstable |
| verification uncertainty | unclear how to check the result (open-ended spec) |
| constraint density | many coupled constraints → high chance of losing an invariant |
| subsystem coupling | several subsystems must change together |
| novelty | task is new relative to known patterns |
| hypothesis conflict | agents give mutually exclusive explanations |
| repair history | two repair loops in a row failed |

Initial form is a rule-based weighted sum with a threshold; coefficients are a starting
guess. They are NOT hand-tuned forever — they are LEARNED from real task history (below).

## Escalation eval dataset — the moat

From the first day the mechanism exists, record every task where the system called the
frontier role: the signals, the decision, the outcome. Compute false-positives (frontier
called but a cheap plan would have sufficed) and false-negatives (cheap plan failed where
frontier would have caught it). After a few hundred real tasks, train the routing policy
to answer the key question: **when does ~$5 of expensive reasoning save ~$20 of cheap
swarm compute AND raise success probability?** This learned router — plus the failure
taxonomy — is the defensible part of the harness, not the role prompts or the model pins.

## The real comparison (frames the benchmark)

Wrong question: "is GLM-5.3-Flash better than Opus 5?" (obviously no, as a solo model).
Right question: "is the SYSTEM (routing + parallel search + specialization + independent
criticism + environmental verification + frontier escalation) better than Opus-5 → task?"
A single very smart trajectory can lose to a well-routed system of cheaper models with
independent criticism and rare frontier escalation. This is exactly what the matched-budget
Harness Report (see benchmark plan) must measure: system vs one-smart-trajectory at equal
budget, on verified success and false-success.

## Operator directives incorporated (2026-08-30)

1. **Orchestrate DeepSeek across opencode + commandcode NOW.** The operator holds DeepSeek
   subscriptions on both providers. This is FAILOVER Slice-2 (model-centric endpoints) and
   it is pulled forward: it is simultaneously the failover bug fix (a model served by >1
   provider fails over when one provider's quota/auth dies) AND the enabler for a
   provider-diverse cheap planner market (two independent DeepSeek endpoints + GLM +
   MiniMax). See docs/design/FAILOVER-risk-aware-allocation.md.
2. **MiniMax M3-Pro ships 2026-09-03** → candidate binding for `frontier-resolver` /
   stronger planner on complex tasks. Slots into the role when it lands and shows evals.
3. **Astra (OpenAI upcoming; agentic coding + cybersecurity)** — as of 2026-08-30 not
   released, reportedly held back over cyber-capability level; no public general-reasoning
   evals. Treat as a speculative `frontier-resolver` binding: roles-primary means we bind
   the role to grok-4.6 today and swap to Astra only if/when it ships and measurably wins.
   Track its release and evals; do not architect around a name.

## Sequencing (under the live-tool-works-now priority)

Bugs before features. The frontier vision is a feature arc, built incrementally and
measured. Order:

1. **Incident-cluster bug fixes FIRST** — ops-remediation (Etap 2, awaiting operator
   green light), FAILOVER Slice-1a (risk-floor) / 1b (cause-attribution). These fix the
   live tool the operator works with today.
2. **FAILOVER Slice-2 (model-centric endpoints), pulled forward** — DeepSeek on
   opencode + commandcode. Bug fix + enabler for the planner market.
3. **Cheap advisors (risk-recon lens)** — first cheap-parallel increment, profile-scoped,
   measured via R1. See docs/design/CHEAP-ADVISORS-risk-recon.md (to be written, WP-CA0).
4. **Planner market** — generalize advisors into planning: `planner-a/b/c` roles +
   `plan-comparator`; measure whether disagreement actually predicts bad decompositions.
5. **Frontier escalation** — `frontier_score` (rule-based first) → escalate to
   `frontier-resolver`; escalation eval dataset recorded from day one.
6. **Learned router** — after enough real tasks in the escalation dataset, train the
   routing policy. The moat.

## Non-goals and cautions

- Do NOT build the learned router before the escalation dataset exists (cold start).
- Do NOT run the planner market on every task — N planners has cost; trigger it only on
  uncertain tasks, same discipline as the cheap-advisors trigger (medium+ complexity).
- Do NOT let frontier become the executor — the entire economic point is rare meta-planning
  over cheap execution.
- Do NOT block on Astra — roles-primary; bind `frontier-resolver` to the best available
  model now (grok-4.6), swap later.
- Do NOT hardcode any of this as always-on — every increment is an R1 ablation hypothesis;
  a stage that does not earn its keep does not enter the default composition.
