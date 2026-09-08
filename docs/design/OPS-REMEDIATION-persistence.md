# Ops-remediation persistence design note

> **Candidate work package (WP).** This is a candidate WP for the post-v0.1.0 incident-cluster workstream. Queue ordering is pending a separate operator decision; this note does not pre-decide sequencing.

## The motivating incident

A production data-platform remediation session exposed a completion failure rather than a diagnosis failure. Both executors diagnosed the same root cause: disk exhaustion on the data platform, with the columnar store at 98.44% full. The difference was remediation persistence, not diagnosis. The strong executor continued after a destructive operation was blocked: it used a data-preserving rename rather than the blocked deletion, used a query-level setting rather than a server configuration change, and used a recovery workflow rather than manual application steps. It applied hung loads through recovery paths and escalated only a large, genuinely irreversible partition deletion to the operator. The weaker executor treated “the deletion was blocked” as “there is nothing left to do” and stopped.

The controller analysis gives the calibration principle for this WP: **“exhaust safe reversible remediation autonomously; escalate only the irreversible.”** This is an executor-remediation contract, not a claim that the current routing layer can make an executor stronger or safely authorize every production operation.

### Current completion and gating contract

The current layer treats a terminal non-empty child result as a stage attestation; the retrieval path classifies stable, non-empty output and records the result (`grokbuild/hook.py:1077-1128`). Pre-tool decisions emit one terminal allow or deny outcome, and their decision, reason, and telemetry are assembled by `_finish_pre_tool` (`grokbuild/hook.py:429-447`). Spawn and write gating, including the recipe returned when a requested action is denied, is dispatched in `grokbuild/hook.py:524-620`.

When no implementation tier is available, the current pipeline records a warning and degrades to observe-only with no gate (`grokbuild/pipeline.py:1391-1404`). A subagent is exempt from the conductor's gate so that child work cannot deadlock; this is a cooperative-not-sandbox boundary, not enforcement of the child's actions (`grokbuild/hook.py:450-453`, `grokbuild/hook.py:157-160`). The proposed WP must preserve that honest boundary and must not represent routing hints as a sandbox.

## Structural gaps

1. **Denials provide recipes but not safe alternatives.** The executor-visible denial payload currently comes from `_deny` and carries only `decision` plus a string `reason` (`grokbuild/hook.py:226-230`). The telemetry record is assembled separately by `_finish_pre_tool` and carries the decision, reason code, and fields (`grokbuild/hook.py:429-447`); denial dispatch supplies the recipes (`grokbuild/hook.py:524-620`). Neither surface carries a bounded hint about a safe alternative. Slice-A must extend both surfaces with an optional structured `safe_alternative` field, applying `redact()` before output to the blocked executor and before telemetry persistence. This is the natural seam, and all strings and nested values must remain recursively redacted and truncated by `grokbuild/redact.py:55-80` so the hint reaches the executor without violating the stated boundary.
2. **Failure evidence lacks a cause class.** Failure records do not distinguish model, environment, or authentication causes. This is the diagnosis gap proven by the H1 incident analysis and addressed by FAILOVER Slice-1b. Generic failure evidence cannot tell an executor or operator whether to retry, change the remediation path, repair the environment, or escalate credentials.
3. **Remediation attempts do not persist across turns.** The layer persists execution debt across turns: `latest_session_debt` finds a recent incomplete execution (`grokbuild/state.py:601-608`), and the Stop hook blocks on session debt when configured to do so (`grokbuild/hook.py:774-791`). No equivalent record preserves an outstanding remediation obligation, attempted alternatives, outcomes, or the remaining irreversible step. A turn boundary can therefore erase the fact that the first blocked operation was only one branch of the remediation plan.

The state-v8 compatibility boundary is explicit: the golden test compares exact serialized bytes to `tests/fixtures/state-v8-golden.json` (`tests/test_state_golden.py:20-23`, `tests/test_state_golden.py:144-149`). Any persistence implementation must either use an additive, absence-by-default representation with proof of byte stability or use a separate versioned store; this note does not choose the storage migration.

## Goals and boundaries

The goal is to make remediation persistence and escalation calibrated: preserve enough typed evidence for an executor to continue through safe alternatives across turns, while making irreversible escalation explicit and reviewable. The design is a sketch for a candidate WP, not a wire-level specification.

The boundary is the current cooperative routing layer. It may classify denials, record evidence, carry bounded hints, and retain remediation debt. It cannot promise a sandbox, infer that a model is capable of a behavior it did not exhibit, or authorize an irreversible operation merely because no alternative was found. Operator context must accompany any irreversible escalation: blocked action, attempted safe alternatives, evidence and cause class, scope or impact estimate, and the exact decision requested.

## Proposed design

### Denial payload with a safe alternative

Slice-A adds an optional bounded `safe_alternative` object to both the executor-visible `_deny` output and the persisted `_finish_pre_tool` telemetry record. The implementation must call `redact()` on the hint before emitting the denial output and again at the persistence boundary, preserving the cooperative and redaction boundaries. A sketch of its meaning is:

- `kind`: a stable category such as `rename`, `query_setting`, or `recovery_workflow`;
- `description`: a redaction-safe, data-preserving next path;
- `reversibility`: `reversible` or `unknown` (unknown never authorizes autonomous action);
- `operator_context`: why the alternative is safe and what evidence is needed to use it.

The field is a hint, not permission. Its values should come from a reviewed, data-driven mapping table rather than free-form model claims. A missing, stale, malformed, or unknown hint means the executor must not assume that a safe path exists.

### Failure-cause class

Add a cause class to failure evidence with the FAILOVER Slice-1b vocabulary: `auth/credential`, `environment/infrastructure`, `model`, or conservative `unknown`. Slice-1b is the authoritative cross-reference for classification, circuit effects, and operator-visible attribution. This WP consumes that evidence; it does not redefine the failure taxonomy.

### Remediation debt

Persist a session-scoped remediation record that identifies the obligation, its current branch, safe alternatives considered, attempt outcomes, unresolved operations, reversibility classification, evidence references, and last update. It should mirror session-debt mechanics without conflating remediation with verification: the record survives a turn and can be reconciled when a later attempt supplies an outcome. The record must be bounded, redaction-safe, and compatible with state-v8 migration decisions.

### Calibrated escalation

Apply one rule consistently: reversible and data-preserving remediation is autonomous when the executor has a valid safe path and evidence; a genuinely irreversible operation is escalated to the operator with context. “Blocked destructive operation” is not a terminal “no safe actions left” result. The executor must search the declared safe alternatives and record each attempt before declaring exhaustion. An unknown reversibility classification is not reversible for this rule.

## Slices

### Slice-A: safe-alternative hints on destructive-operation denials

Create a reviewed, data-driven mapping table from bounded denial reason and operation classes to safe-alternative hints. Emit the optional structured field at the denial seam, preserve the existing reason and telemetry record, and redact before persistence. The first mappings cover rename instead of deletion, query-level setting instead of server configuration change, and recovery workflow instead of manual application. Slice-A is first because the first blocked operation is the immediate false-terminal signal.

### Slice-B: failure-cause class fields

Add cause-class fields to failure evidence and use the FAILOVER Slice-1b rules for conservative defaults and model-circuit effects. Slice-B follows Slice-A in this workstream's dependency rationale because persistence of an alternative attempt is more useful when its outcome has an attributable cause; its implementation may proceed independently if the operator chooses. It must not claim the motivating incident's diagnosis gap is solved by Slice-A.

### Slice-C: remediation-debt persistence across turns

Add the bounded session-scoped record and reconciliation mechanics, analogous to execution debt. Carry outstanding safe alternatives and unresolved irreversible escalation across turns, with an explicit policy decision about whether this debt blocks Stop. Slice-C follows the evidence shape in Slices A and B so records can identify both the alternative and why an attempt failed. Queue ordering remains pending the operator decision.

## Incident mapping

- **Slice-A:** The first blocked partition deletion would have produced a typed hint that a data-preserving rename path remained. The executor would have been told to distinguish a blocked destructive branch from exhaustion of safe actions.
- **Slice-B:** The executors had the same diagnosis, so this incident does not demonstrate a diagnosis difference. The broader failure-cause attribution gap belongs to Slice-B and FAILOVER Slice-1b territory; it would improve evidence around model, environment, and credential causes but would not itself make the weaker executor stronger.
- **Slice-C:** The remediation obligation and successful alternative attempts would have survived the turn boundary, preventing the blocked deletion from becoming an implicit terminal state. It would also have retained the large partition deletion as an explicit operator escalation rather than losing it among completed steps.

The incident supports a persistence and contract change; it does not prove that every safe alternative can be generated or that every production operation can be classified automatically.

## Open questions for the operator

1. What is the model-ability ceiling? A stronger executor's behavior is partly outside configuration scope; the behavioral frame is in scope, while capability claims require evaluation evidence.
2. What production risk-classification policy should govern “safe,” especially for medium-risk operations? Cross-reference the FAILOVER open questions on production risk and escalation rather than silently choosing a new threshold here.
3. Should remediation debt block Stop in the same way as execution debt, or should it produce a warning and operator escalation without blocking? The current execution-debt behavior is visible in `grokbuild/state.py:601-608` and `grokbuild/hook.py:774-791`.
4. Who owns and reviews the data-driven safe-alternative mapping table, and what evidence expires a hint?
5. Which storage option preserves state-v8 golden bytes while providing bounded cross-turn retention?

## Migration and tests

Migration should begin with additive, absence-by-default evidence fields and a separately versioned remediation-debt store unless an operator-approved state migration proves equivalent compatibility. Preserve existing denial reasons, telemetry generation, and observe-only behavior until each slice has a deterministic gate decision. Redaction remains mandatory at the existing boundary (`grokbuild/redact.py:55-80`); no prompt, credential, or unrestricted tool input should enter the new record.

Expected deterministic coverage includes:

- denial fixtures with no hint, one valid hint, malformed hints, unknown reversibility, and nested values requiring redaction;
- mapping-table tests proving blocked destructive operations do not become “no safe actions left” without exhausting valid alternatives;
- failure evidence for all FAILOVER Slice-1b cause classes and conservative `unknown` handling;
- cross-turn creation, update, reconciliation, expiry, and duplicate-attempt tests for remediation debt;
- Stop behavior tests for both operator-selected policies, while preserving existing execution-debt behavior;
- state-v8 golden-byte tests proving the chosen migration does not emit accidental fields (`tests/test_state_golden.py:144-149`).

The documentation acceptance check for this candidate is `render_docs` plus release and repository checks; no implementation or test changes are part of this note.

## Related approaches

This WP complements FAILOVER-risk-aware-allocation: FAILOVER Slice-1 addresses risk floors and explicit escalation, and Slice-1b addresses failure-cause attribution. It does not replace those decisions. It also relates to R2's typed task contracts because remediation evidence should state criteria and unresolved items rather than rely on a free-form completion claim. R6 is a boundary reminder: child-level enforcement is a separate hardening path, not a promise made by remediation hints. R9 supplies the ablation discipline needed to measure whether persistence and hints earn their cost.

## ROADMAP mapping

This is cross-cutting, not an orphan:

- **R6 — Child-level enforcement:** the roadmap says the cooperative-not-sandbox boundary leaves writable children outside per-action enforcement and proposes inspectable worktrees, allowlists, command/network policy, diff manifests, and merge gates (`docs/ROADMAP.md:81-88`). This WP records that limitation and does not turn safe-alternative hints into enforcement.
- **R2 — Typed task contracts / evidence protocol:** the roadmap says completion is currently a terminal non-empty result used as stage attestation and proposes typed `TaskSpec`/`TaskResult` with independently checked criteria and unresolved items (`docs/ROADMAP.md:25-31`). Denial hints, cause classes, and remediation debt are typed evidence that supports that direction.
- **R9 — Ablation discipline:** the roadmap says shadow mode and telemetry should earn or remove stages based on paired outcome and cost measurements (`docs/ROADMAP.md:121-130`). Each slice needs ablation evidence showing that persistence and alternative hints improve remediation completion without increasing unsafe actions or unnecessary operator load.

## Scope

The candidate scope is the routing-layer contract, denial evidence, cause attribution integration, bounded remediation-debt persistence, migration design, and deterministic validation described above. It is a post-v0.1.0 incident-cluster candidate. Any implementation must preserve the current cooperative boundary, redaction boundary, and state-v8 compatibility unless an explicit migration is approved.

## Non-goals

- No sandbox promises or claims of child-process enforcement.
- No automatic authorization or execution of genuinely irreversible operations.
- No adaptive topology (R7).
- No ML scheduler.
- No queue ordering decision.
- No claim that routing configuration alone can reproduce a stronger executor's capabilities.
- No change to production systems, operator policy, or live remediation in this design note.
