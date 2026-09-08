# R2 typed task contracts and evidence protocol

## 1. Frame and current completion contract

Today, completion for a task id is a terminal, non-empty result used as a stage attestation. The current chain is precise: `grokbuild/payloads.py` classifies a terminal non-empty payload as `success`; retrieval handling in `grokbuild/hook.py` passes `success=True`; and `record_retrieval_result_tx` appends the bound stage key to `completed` (`grokbuild/transactions.py:753-760`). The transaction caveat is explicit: “The track's `completed`/`failed` lists record received results, not proof that a child verified its work.” (`grokbuild/transactions.py:623-630`). The runtime therefore records a child's claim of completion; it does not independently establish that the claimed work satisfies a task contract. This is a cooperative-not-sandbox system: the child is a trusted reporter for purposes of communication, not an oracle whose claims are accepted without checks.

The current execution boundary is also explicit in `grokbuild/pipeline.py`: “The hook remains responsible for the final deny/block decision because it knows what the pipeline cannot (spawn completion this turn, and the real Stop reason).” In `grokbuild/state.py`, `ExecutionTrack` documents that spawnable stages are tracked through `requested`/`completed`/`failed`, while deterministic verification stages are tracked separately in `verified` (`grokbuild/state.py:202-209`). Its required-stage calculation treats a non-parallel stage as complete when its role is in `completed` (`grokbuild/state.py:289-297`). These are stage-attestation facts, not evidence verification.

R2 changes completion from “a terminal non-empty result exists” to “the runtime has verified typed evidence against the applicable contract.” A terminal non-empty result **NO LONGER completes a stage by itself**. Only runtime-verified evidence may complete it.

## 2. Ratified contract triangle

R2 defines one typed evidence protocol with three mutually reinforcing parts.

### 2.1 Typed `TaskSpec`

A `TaskSpec` is attached to the stage/task before the child spawns. It declares up front:

- the task identity and contract version;
- the success criteria, including the criteria kind and expected values;
- expected artifact paths;
- the permitted `path_scope`; and
- the checks that must run, including deterministic offline checks where applicable.

The spec is the runtime's statement of what “done” means. It is immutable for the task attempt after spawn, and reviewers cite the same spec rather than reconstructing acceptance from a child's prose.

### 2.2 Typed `TaskResult`

A `TaskResult` is the child's structured claim and contains at least:

- `status`;
- `changed_paths`;
- `artifacts`;
- `checks`;
- `criteria`; and
- `unresolved` items.

The result records what the child says happened. It does not certify that the claim is true. A malformed, incomplete, or internally inconsistent result is evidence failure, not successful completion.

### 2.3 Contract manifest hardening

A machine-checkable contract manifest lists the contracts that `TaskSpec` instances and reviewers cite. It provides stable identities and versions for the checks and criteria used by a route. Manifest hardening is a linked workstream following the post-v0.1.0 hardening direction; R2 relies on that manifest boundary but does not design or implement the manifest here.

## 3. Independent runtime verification

After receiving a typed result, the runtime independently verifies it against the pre-attached spec and the applicable manifest entry. At minimum it must verify:

1. every declared artifact and changed path exists when existence is required;
2. every path is inside the task's `path_scope`;
3. the observed diff matches the declared `changed_paths` (including additions, modifications, deletions, and omissions according to the contract);
4. declared checks actually pass, using deterministic checks that are runnable offline where possible; and
5. declared success criteria are verified by the runtime rather than accepted from the result text.

Unresolved items block completion or route the task to repair. A result with `status: success` cannot override a failed path, diff, check, or criterion verification. Conversely, a terminal child response without a valid typed result cannot supply the evidence needed for the new completion decision.

Every persisted evidence write crosses `redact()`. Only bounded, structured evidence fields and verifier outcomes may be persisted. Raw prompts, child transcripts, verifier source or hidden assertions, unbounded command output, and unbounded artifact contents are explicitly prohibited from persistence. As in R1, the verifier runs outside the tested run's visibility; the tested run sees only pass/fail, never verifier source, hidden assertions, or detailed verifier failures. Runtime verification validates claims against observed repository/worktree state. It does not enforce child honesty and is not a sandbox.

The resulting state transition is therefore evidence-based: the runtime records a stage as complete only after the independent verifier accepts the `TaskSpec`/`TaskResult` pair and required checks. A failed verification remains failed or pending repair, with its reason recorded for routing and review. This preserves the existing distinction between spawnable stage progress and deterministic verification progress while changing the acceptance rule for the former.

## 4. Migration and compatibility

R2 changes completion evaluation, not record serialization. State version 8 and log schema version 4 remain **BIT-STABLE**: their formats, field meanings, ordering, and serialized bytes are unchanged. The typed layer rides on the existing state and log records, using additive interpretation and evidence references rather than changing their wire formats.

The migration sequence is:

1. define and version the typed `TaskSpec`, `TaskResult`, and manifest references;
2. attach an immutable spec before each child spawn;
3. collect the child's typed result alongside the existing terminal-result observation;
4. run independent path, diff, check, and criteria verification;
5. make completion decisions from the verification outcome, while retaining existing state/log serialization; and
6. add fixtures for successful, empty, malformed, wrong-path, and mismatched-diff results before enabling the stricter decision for all routes.

The compatibility suite intentionally pins today's “terminal non-empty completes” behavior as preserved legacy-policy behavior. This includes the spawn-result predicate, background-ack-never-lifts-gate, and parallel-retrieval-correlation blocks in `tests/test_security_regressions.py`; `linear-background-chain` and `terminal-background-reopens-completed-stage` in `tests/test_task_payload_contracts.py`; and member-result assertions in `tests/test_state_machine.py`. Strict-evidence coverage will be added as separate new tests rather than changing these expected behaviors.

The golden state test, `tests/test_state_golden.py`, MUST remain byte-identical before and after R2. In particular, changing completion semantics must not change serialized state bytes or log bytes. The golden test is a migration gate, not merely a regression convenience.

During transition, a child that emits a plain result with no typed payload carries `typed_evidence_missing`. This is a redacted field on a versioned, out-of-band result event / R2 run record; it is explicitly NOT part of state v8 serialization and explicitly NOT part of log schema v4. Legacy-policy tasks continue calling the existing transactions with today's success classification unchanged. Until a later, versioned route/contract policy mechanism flips a task from legacy policy to evidence-required policy after compatibility validation, missing evidence is visible metadata, not a completion blocker. Once that versioned policy is enabled for a task, the marker identifies an untyped result that cannot satisfy the strict evidence-required completion decision and must remain pending or enter repair. This stance allows existing children to continue reporting while preventing an untyped claim from silently receiving the new completion guarantee; compatibility is a migration aid, not proof.

Under the current legacy policy, an `r2-evidence-v1.jsonl` record is expected to carry `policy: "legacy"`, `typed_result: false`, `verified: false`, `reason_codes: ["not_evaluated"]`, and `notes: "legacy policy: evidence is recorded, not enforced"`. This zero signal is intentional until a versioned evidence-required profile is enabled; the writer records the observation without changing completion behavior.

## 5. Offline and live validation

### Offline CI

Offline tests validate `TaskResult` schema and cross-field consistency, exercise path and diff checks against a synthetic environment, and use verifier stubs for deterministic checks. Cases cover successful, empty, malformed, wrong-path, mismatched-diff, and unresolved results. These tests make no provider calls and consume no live tokens.

### Live protocol

Live validation uses actual runs with real children under the manual procedure in [`docs/eval/live-protocol.md`](../eval/live-protocol.md). The operator freezes the task spec, contract/manifest references, controls, and run identity before spawning; the child runs in the isolated task worktree; and the runtime records observed typed results and verification outcomes after the run. Controller-owned verifier details remain outside the tested run and repository. Valid, invalid, and missing evidence are reported separately rather than inferred from exit status.

## 6. Non-goals and boundaries

- **No child-side enforcement.** Children remain trusted reporters whose claims are independently verified. Cooperative-not-sandbox still applies; R2 does not contain a malicious child process.
- **No SWE-bench integration.** R2 defines the evidence protocol and its validation fixtures, not a public benchmark adapter.
- **No adaptive topology.** Stage selection and graph adaptation remain outside this work; adaptive topology is reserved for R7.
- **No manifest design here.** Contract-manifest hardening remains the linked post-v0.1.0 workstream described above.

R2 is complete only when typed evidence is evaluated independently at the runtime boundary, the compatibility behavior is explicit, and the state v8/log v4 golden serialization remains bit-for-bit unchanged.
