# R1 evaluation harness design note

## 1. Task set, fixture governance, and verifier isolation

R1 evaluates repository tasks, not prompt-answer quality in isolation. Each public task fixture is a versioned file at `eval/tasks/<id>.json`. Its minimum JSON contract is:

```json
{
  "schema_version": 1,
  "id": "calc-fix-001",
  "version": 3,
  "prompt": "...",
  "task_class": "implement",
  "complexity": "low|medium|high",
  "path_scope": ["src/calc.py", "tests/test_calc.py"],
  "rubric": {"ref": "eval/verifiers/calc-fix-001/rubric.md", "version": 2},
  "hidden_verifier": {"ref": "controller-only", "version": 2},
  "acceptance": [
    {"kind": "pytest", "spec": "test_calc.py::test_add"},
    {"kind": "file_contains", "spec": "src/calc.py:return a + b"}
  ]
}
```

`id` plus `version` pins the exact graded artifact. `path_scope` defines the writable and allowed path set used by the files-changed-outside-scope metric; every changed file outside that list counts as out of scope. `rubric.ref` and `hidden_verifier.ref` are references resolved in the controller-only store, and their independent versions make grading reproducible; the illustrative rubric reference is not public verifier material. A fixture contains no verifier material: neither verifier source, implementation details, hidden assertions, expected patch fragments, nor detailed failure messages.

The rubric and acceptance criteria are frozen before a trial starts. The freeze is recorded in the run record as the fixture identifier and version, rubric version, acceptance-criteria version, and freeze timestamp or immutable digest. A task cannot be edited in place after that freeze; a changed task is a new fixture version and a new comparison cell.

Hidden verifiers are controller-owned, outside both the agent-visible sandbox and the tested worktree. The R1 convention is `$GROK_R1_CONTROLLER_HOME/verifiers/<task-id>/<fixture-version>/`, configured outside the sandbox and excluded from its filesystem view. The controller creates or selects the verifier before the trial, runs it only after the tested run has terminated, and retains its source and detailed failure output. The tested run receives only a final pass/fail result (and the ordinary run outcome); it never receives verifier source, hidden assertions, or detailed failure output. This isolation is a design boundary, not a claim that the current cooperative hook can enforce a malicious child process; the repository documents that limitation in its security model.

Every task passes an independent human quality gate before publication. The checklist records:

- **Depersonalization:** remove people, organizations, repository names, identifying project facts, and proprietary facts; replace or generalize them without changing the engineering problem. This is a separate review from `corpus_sync`. Corpus synchronization is precedent for secret redaction, collapse, truncation, and manual adjudication, not for depersonalization.
- **Rubric clarity:** a reviewer can distinguish complete, partial, and incorrect results from the frozen task statement and acceptance criteria.
- **Verifier validity:** the hidden verifier actually discriminates a correct solution from a plausible but incorrect one, has a documented expected outcome, and is itself reviewed before the fixture is frozen.

The task quality record is controller-owned with the fixture review evidence. It is not exposed to the tested run.

## 2. Variants

A variant is an exact transformation of a base composition plus an invariant table. The table is versioned with the experiment and states what is removed, what remains, and which offline assertions must continue to hold. The baseline is **single-best-agent**: one strong tested agent, no stages, with the same task fixture, initial state, permissions, and termination rules as other variants. It is not a one-member reconstruction of the pipeline.

| Variant namespace | Exact transformation | Invariants and offline assertions |
| --- | --- | --- |
| `single-best-agent` | Replace the composed pipeline with one tested agent and no recon, planning, review, or other stages. | One tested model stage; no pipeline stages; same task, initial state, permissions, and verifier timing. |
| `full-pipeline` | Use the normal composition selected by policy for the task. | Required stage presence, member IDs, profile widths, and topology shape match the resolved route. |
| `no-recon` | Remove the recon barrier and start implementation directly. | The implementation stage remains; gating composition is updated to remove recon dependencies, and no recon member may run. Other applicable stages and verifier timing remain unchanged. |
| `no-LLM-review` | Remove LLM review stages after implementation. | Recon remains; deterministic verifiers remain and run after termination; implementation, recon, and verifier ordering stay valid. |
| `verifier-only` | Remove every model stage except the single tested agent; do not run recon, planning, review, or auxiliary model stages. | Exactly one tested agent remains, deterministic verifiers remain, and the verifier executes only after that agent terminates. This is not a verifier-only execution with no agent. |
| `same-provider-reviewer` | Pin the reviewer to the same provider as the implementation model while preserving the review role and effort contract. | Same task, implementation, recon, verifier, budget, and topology shape except for the reviewer provider; record resolved provider identity. |
| `cross-provider-reviewer` | Pin the reviewer to a distinct provider from the implementation model. | Same controls and stage shape as same-provider review; distinctness and availability are recorded, not inferred from role names. |
| `fixed-topology` | Use the pre-run resolved topology without adapting it from intermediate outcomes. | Composition and member set are frozen before the run and all required stages are either executed or reported as failed. |
| `adaptive-topology` (reserved) | Select or remove stages from intermediate outcomes. | The R1 interface must accept a topology variant namespace and record topology decisions, but R1 does not execute this row. Adaptive topology is R7 and depends on R1 evidence; deferring it avoids a dependency cycle in which R1 evaluates a mechanism it has not yet specified. |

The same task is paired across variants wherever possible. A transformation must not alter the task, hidden verifier, initial task state, environment snapshot, or trial budget. Any unavoidable unpaired trial is marked in the run record and excluded from paired deltas, but remains an explicitly reported trial.

## 3. Metrics

The split is deliberate. **OFFLINE** metrics are deterministic, zero-token assertions about ablation effects on composition and topology: member counts, stage presence, topology shape, and profile widths. They are tested like ordinary repository runners. **LIVE** metrics are outcomes derived from real model runs and therefore consume money and exhibit stochasticity; they require the manual protocol below and an INTERACTIVE host.

| Layer and metric | Numerator | Denominator | Source of truth | Missing-data behavior |
| --- | --- | --- | --- | --- |
| LIVE task success | Trials meeting the frozen acceptance criteria | Valid, completed trials | Run record plus controller adjudication and verifier result | Mark unknown if adjudication is absent; never infer success from exit status. |
| LIVE hidden-test success | Trials whose post-run hidden verifier passes | Valid, verifier-executed trials | Controller verifier result | Mark unavailable when the verifier did not execute; do not score the trial as a pass or fail. |
| LIVE false-success | Trials reported successful by the tested run but failing frozen acceptance or hidden verification | Trials reported successful by the tested run | Run outcome, adjudication, and verifier | Mark unknown if either required judgment is missing. |
| LIVE false-BLOCKED | Trials reported or terminated as `BLOCKED` although the frozen task and hidden verifier would pass under post-run adjudication | Trials with a `BLOCKED` outcome and a completed adjudication | Run record, state outcome, and verifier | Mark unknown when a blocked artifact cannot be adjudicated. |
| LIVE unnecessary-agent rate | Runs with an executed agent/stage that the predeclared variant says is unnecessary for that cell | Valid runs | Run record resolved topology versus variant invariant table | Report zero only when the topology is complete; otherwise mark incomplete. |
| LIVE tokens | Total reported input and output tokens consumed | Valid live trials | Provider/model usage records captured in the run record | Report missing usage separately; do not substitute an estimate. |
| LIVE wall-clock | Elapsed seconds from tested-run start to tested-run termination | Valid live trials with both timestamps | Run-record monotonic or paired timestamps | Exclude from aggregate latency if either timestamp is missing and count the omission. |
| LIVE actual tool calls | Tool calls actually observed during the run | Valid live trials | `state.json` and `route.jsonl`, reconciled with the run record | Mark telemetry incomplete on disagreement; never reconstruct calls from planned topology. |
| LIVE retries | Retry attempts actually started | Valid live trials | `state.json` and `route.jsonl` | Missing telemetry yields an incomplete metric, not zero. |
| LIVE stop blocks | Stop events that blocked completion | Valid live trials | `state.json` and `route.jsonl` | Count only observed events; missing Stop telemetry is reported as incomplete. |
| LIVE files changed outside scope | Runs with one or more changed paths outside the fixture's declared scope | Valid runs with a captured final diff | Final worktree diff plus run record | Mark unknown when no final diff was captured; do not treat no diff as in-scope. |
| OFFLINE composition assertions | Passing topology snapshots | Snapshot cases | Deterministic composition output and test expectation | A malformed snapshot is a failed offline case, not a skipped case. |
| OFFLINE synthetic trace accounting | Synthetic traces satisfying expected stage/member accounting | Synthetic trace cases | Test fixtures and deterministic accounting runner | Fail the case on malformed input; no live-run inference. |

A trial with an environment or setup failure is **INVALID**, not a scored failure. Invalid trials are reported separately in run records with the failure class and evidence; they are never silently scored, dropped, or used as a denominator. Live aggregates publish valid, invalid, and missing-data counts for every cell.

## 4. Experiment controls

Before any comparison, the controller locks and records:

1. task fixture ID and version, including rubric and acceptance-criteria digests;
2. variant namespace and definition version, plus the resolved invariant table;
3. repository and environment snapshot, including the repository commit, installer revision, runtime version, operating-system/host label, and sandbox configuration;
4. model, provider, and reasoning effort for every role, including the tested agent and reviewer;
5. tool permissions, verifier command identity, network policy, and writable-path policy;
6. initial task state, including the clean worktree snapshot and task-specific seed/input state;
7. trial ordering, randomization seed where applicable, and repetition count; and
8. cross-variant pairing IDs and the rule used to match trials.

These locks live in each run record under `fixture`, `variant`, `environment`, `roles`, `permissions`, `initial_state`, `ordering`, and `pairing`. The record also stores immutable digests of those objects so a readable label cannot silently change. A comparison is valid only when its locked controls match, apart from the intended variant transformation.

## 5. CI versus manual execution

### Offline CI suite

The offline suite is deterministic, uses zero tokens, and runs in CI. For each task fixture and variant it snapshots composition: stage presence, ordered topology shape, barrier member count and IDs, resolved recon/research profile widths, and verifier placement. Ablation diffs are ordinary runner tests: `no-recon` must remove only its declared recon dependency while retaining implementation, `no-LLM-review` must retain recon and deterministic verifiers, and `verifier-only` must retain only the tested agent among model stages. Synthetic trace accounting tests planned-versus-observed counters without contacting a model provider.

The current tree has no R1 task-fixture or run-record implementation. The existing 50-case corpus and composition tests remain the offline regression net; R1 adds the task-fixture layer and, later, the run-record layer. No existing test changes are part of this iteration.

### Live manual protocol

LIVE is a documented manual protocol mirroring the H1 sandbox procedure, not automated orchestration. An operator prepares a throwaway `HOME`, installs the tree into that home with the repository installer, and provides only the operator's pre-existing authentication through explicitly selected symlinks. The task is copied into an isolated worktree, the locked run record is initialized, the selected variant is run interactively, the run is allowed to terminate, and only then does the controller execute the hidden verifier outside the sandbox. The operator records model/provider/effort, tool permissions, timestamps, observed telemetry, final diff, verifier result, and invalid-run evidence.

H1 is an **EXTERNAL ratified premise**, not tree-proven evidence. Its procedure is summarized by the quoted command/result pair: “install with `HOME=<throwaway-home> ./scripts/install.sh`, with authentication supplied by symlinked session files; result: the isolated installation completed and the interactive sandbox run was usable, but live automation could not be delegated by the host.” This note relies on that external finding only to justify the manual protocol and its host limitation; the current repository proves the installer and isolated installer test, not the H1 live result.

Live records are stored under `eval/runs/`. `eval/runs/` **MUST be added to `.gitignore` before any live record is written** because run records may contain task-specific diffs and provider telemetry. The implementation WP adds `/eval/runs/` to `.gitignore` as part of the run-record layer. Source-controlled fixtures and sanitized aggregate reports can be reviewed without publishing raw run artifacts. The controller-owned verifier records remain outside the repository.

The run-record JSON schema is versioned and contains at least: `schema_version`, `run_id`, `pairing_id`, `fixture` (ID, version, rubric digest, acceptance digest, freeze record), `variant` (namespace, definition version, invariant digest), `environment` (repository commit, host, runtime, sandbox and installer snapshot), `roles` (role, provider, model, effort, resolved availability), `permissions`, `initial_state`, `ordering`, `start`, `termination`, `outcome`, `invalid` (boolean, class, evidence), `telemetry` (state/route/decision references and completeness), `usage` (tokens and source), `tool_calls`, `retries`, `stop_blocks`, `changed_paths`, `verifier` (controller result and timing, never verifier source), and `adjudication`. Detailed verifier failures are controller-only and are not copied into the tested run record.

A small offline summarizer will later read only run records, validate schema and control locks, separate invalid and missing data, and emit per-task and per-variant counts, paired deltas, uncertainty/repetition counts, and cost/latency summaries. It will not run models, invoke verifiers, or rewrite raw records. It is described here, not built in R1 step 1.

## 6. R9 discipline

The R9 hypothesis is **multi-lens recon diversity / profile width**, not “two recon lenses.” The current default width is three (`grokbuild/policy.py:45`), while the recon pool contains six entries (`grokbuild/barrier_lenses.json:3` and following entries). R1 defines the following exact rows in addition to the general variants above:

| Variant namespace | Base composition | Deterministic lens selection | Member-ID rule | Invariant |
| --- | --- | --- | --- | --- |
| `research-width-minus-1` | Configured research width (3) | `pool[:max(1, width - 1)]` | `research/0..N-1` | Stage ID is `research`; all members are required; roles are a subset of the researcher family. |
| `recon-configured` | Configured recon width (3) | `pool[:3]` | `recon/0..N-1` | Stage ID is `recon`; all members are required. |
| `recon-width-minus-1` | Configured recon width (3) | `pool[:2]` (the historical medium shape) | `recon/0..N-1` | Stage ID is `recon`; all members are required. |
| `recon-same-lens` | Configured recon width | Repeat the FIRST configured recon-pool entry (`pool[0]`) for all `width` members, so every member has the same role and lens | Indexed member IDs remain `recon/0..N-1` | Recon diversity is 0. |
| `recon-diverse-lens` | Configured recon width | Pool order, `pool[:width]` | `recon/0..N-1` | The role set equals the roles in `pool[:width]`. |

Every comparison records the resolved profile and selected lens and member IDs, including role and reason, so a nominally identical width cannot conceal a different composition. Pairing is always same task, same locked controls, and one axis at a time: width varies while diversity is held fixed, or diversity varies while width is held fixed. The mandatory `explore` hypothesis remains: removing required explore is evaluated by the `no-recon` row, while implementation still begins directly in that variant. `review-hard` remains mandatory for every medium route in the full pipeline. Consilium escalation is compared with direct escalation as a separate composition decision, with provider availability and escalation reason recorded rather than treated as outcome evidence.

## 7. Non-goals for this iteration

This iteration does not provide automated live orchestration. The host limitation is an H1-proven external premise, and live work therefore remains manual. It does not integrate a public benchmark, claim multi-repository generality, build cost dashboards, or implement adaptive topology. Adaptive topology belongs to R7 and follows R1 evidence.

### Relationship to existing tests

The existing 50-case corpus and composition tests remain the offline regression net. R1 adds the task-fixture layer and, later, the run-record layer; no existing test changes are made in this iteration. This design note describes the boundary and interfaces only; it does not claim that those layers already exist in the current tree.
