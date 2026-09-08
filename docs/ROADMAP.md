# Roadmap

## Frame

Grok Build v0.1.0 is an extracted, deterministic, station-based supervisor for the Grok CLI. This roadmap is a directional map, not a promise of delivery. The current system is cooperative, not a sandbox: it gates a consenting conductor and keeps that conductor zero-write, but it cannot contain a malicious process. The repository has no end-to-end evaluation harness yet. Role pins are data, owned by configuration and provider metadata, rather than an evaluation of model quality. This is a non-binding, additive roadmap for post-release work; none of the items below is implemented by this document.

The current 50-case corpus checks that the router builds the expected pipeline. Shadow mode records decisions without stage denials. Redacted telemetry is written to `route.jsonl` and decision history to `decisions.jsonl`. Circuit breakers, tier and overflow escalation, cross-provider review and consilium, the researcher triad, barrier lens pools, and profile width knobs are present as routing mechanisms. They do not by themselves prove task success, isolation, or security.

## Priorities

### R1 — End-to-end eval + ablation harness

**Problem.** Today the 50-case corpus proves that the router built the expected pipeline, not that the pipeline solves tasks better.

**Why it matters.** Routing complexity should earn its latency, token, and reliability cost, and failures must be distinguished from failures caused by the task environment.

**Proposed shape.** Build a matrix comparing a single-best-agent baseline, the full pipeline, and ablations: no recon; no LLM review; deterministic-verifier-only; same-provider versus cross-provider reviewer; and fixed versus adaptive topology. Measure task success, hidden-test success, false-success rate, false-BLOCKED rate, tokens, wall-clock time, unnecessary-agent rate, and N trials per task. Repeated trials are necessary because the system is stochastic; measure environment changes, not final-text diffs.

**How we'd validate.** R1 validates itself: run the harness on at least one real task where at least one stage proves measurably load-bearing or redundant.

**Rough size.** L

**Depends-on.** None.

### R2 — Typed task contracts / evidence protocol

**Problem.** Today completion for a task id is a terminal non-empty result, used as a stage attestation.

**Why it matters.** A report that says a stage completed is weaker than independently checkable evidence of what was done and whether the declared criteria hold.

**Proposed shape.** Define a typed `TaskSpec` declaring what “done” means and its success criteria up front. Define a typed `TaskResult` containing status, changed paths, artifacts, checks, criteria, and unresolved items. The runtime must not take the result on faith: it independently verifies that paths exist and that the diff matches the claim. Harden this with a machine-checkable contract manifest listing contracts that specs and reviewers cite; the post-v0.1.0 hardening issue is already sketched.

**How we'd validate.** Exercise successful, empty, malformed, wrong-path, and mismatched-diff results; verify that independent checks accept only evidence matching the `TaskSpec` and manifest.

**Rough size.** M

**Depends-on.** None.

### R3 — ResourceClaim + worktree isolation

**Problem.** A barrier member key identifies a participant, but it does not establish file ownership. Parallel writable agents could therefore contend for resources.

**Why it matters.** Distinct stage slots are not a concurrency safety mechanism. Shared writes can create nondeterministic diffs and make review evidence ambiguous.

**Proposed shape.** Add a `ResourceClaim` with `owner_task_id`, a resource glob, `exclusive-write`, and a lease. Keep the explicit rule that parallel writable work in one working copy is forbidden. The future form of writable parallelism is across isolated worktrees, with claims controlling ownership and expiry.

**How we'd validate.** Compete claims over overlapping and disjoint globs, expire and renew leases, and run parallel writable tasks in isolated worktrees; prove that a single working copy is rejected and that merges have attributable diffs.

**Rough size.** L

**Depends-on.** R2.

### R4 — Loop detector

**Problem.** Circuit breakers catch repeated same-role failures, but they do not catch an A-to-B-to-A patch/revert cycle when no formal failure is recorded.

**Why it matters.** A run can consume time and tokens while appearing to make progress even as changes oscillate.

**Proposed shape.** Fingerprint the task spec, changed paths, normalized diff, verifier failure, and delegation edge. Detect recurring fingerprints and patch/revert cycles, then produce a typed block or escalation evidence rather than silently repeating work.

**How we'd validate.** Replay synthetic patch/revert, same-change, and legitimate iterative-fix traces; confirm loops are detected without blocking monotonic progress.

**Rough size.** M

**Depends-on.** R2.

### R5 — Budget hooks

**Problem.** The runtime tracks execution stages and circuit state, but a route does not yet express a general token, cost, or spawn budget gate.

**Why it matters.** Deterministic stage requirements can still expand into unbounded practical cost across retries, review, and overflow.

**Proposed shape.** Add token, cost, and spawn counters with configured limits and a `budget.exceeded` gate. Preserve evidence about which counter exceeded and prevent further work unless an explicit bounded policy permits it.

**How we'd validate.** Use deterministic counter fixtures at zero, boundary, and over-budget values, including retries and barriers; verify the gate and telemetry reason are stable.

**Rough size.** S/M

**Depends-on.** R2.

### R6 — Child-level enforcement

**Problem.** The subagent exemption prevents child deadlock but leaves writable children outside the per-action gate. That is an honest limitation of the cooperative-not-sandbox model.

**Why it matters.** Conductor zero-write does not enforce writable-path, command, or network policy inside a child process.

**Proposed shape.** Use an ephemeral worktree, writable-path allowlist, command and network policy, diff manifest, and merge gate for each writable child. Keep the current cooperative boundary documented while making the child’s permitted effects inspectable.

**How we'd validate.** Attempt allowed and disallowed writes, commands, network operations, and undeclared diffs in an ephemeral child environment; require rejection or quarantine before merge.

**Rough size.** L

**Depends-on.** R2 and R3.

### R7 — Adaptive topology

**Problem.** Current topology is primarily selected from intent, complexity, and risk, with profile-driven widths and lens pools.

**Why it matters.** The same intent can have materially different decomposition, sequencing, oracle, and tooling characteristics; a fixed topology can over-invest or under-invest in stages.

**Proposed shape.** Drive the execution graph from task properties: decomposability, sequentiality, oracle strength, tool density, and expected value, in addition to existing intent and policy signals. Keep topology choices explainable and bounded.

**How we'd validate.** Compare fixed and adaptive graphs in the R1 matrix across tasks with controlled property changes; measure success, cost, latency, unnecessary agents, and false blocks.

**Rough size.** L

**Depends-on.** R1 and R2.

### R8 — Neutral event ABI + adapter split

**Problem.** The v0.1.0 implementation is extracted around the Grok hook contract, so its lifecycle and integration boundary are Grok-specific.

**Why it matters.** A reusable supervisor needs a stable core vocabulary without pretending other host integrations have identical hooks, task semantics, or enforcement.

**Proposed shape.** Define a v1.0 architecture with `multiagent-core` for events, decisions, execution graphs, state, and policies, plus adapters for `grok`, `claude_code`, `codex`, and `opencode`. Use a neutral event vocabulary: `PromptAccepted`, `StageScheduled`, `ChildSpawnRequested`, `ChildTaskBound`, `ChildCompleted`, `ChildFailed`, `VerifierStarted`, `VerifierPassed`, `VerifierFailed`, `RunBlocked`, and `RunCompleted`.

**How we'd validate.** Implement one host adapter against the neutral contract, replay equivalent traces through it, and verify that core decisions and state transitions remain host-independent while adapter-specific capabilities stay explicit.

**Rough size.** XL

**Depends-on.** R2, R3, R5, and R6.

### R9 — Ablation discipline

**Problem.** Shadow mode and telemetry can show what stages run, but current routing has no cross-cutting discipline requiring stages to earn their continued place.

**Why it matters.** More stages are not inherently safer or better. Unnecessary reconnaissance, review, or diversity adds cost and latency and can create new failure surfaces.

**Proposed shape.** Use shadow mode and telemetry to delete stages that do not earn their place, not only to find bugs. Test for removal of mandatory explore, `review-hard` on every medium route, the two-recon-role diversity, and consilium versus direct escalation. Treat removal as an evidence-backed change, not an assumption that fewer stages are always better.

**How we'd validate.** Run the named ablations through R1, publish paired outcome and cost measurements, and retain a stage only when its measured contribution justifies its burden.

**Rough size.** M

**Depends-on.** R1.

## Explicitly out of scope for the roadmap

- **Peer-to-peer swarm or mailbox.** The design remains a centralized supervisor coordinating bounded worker stations; peer protocols would obscure ownership and gate state.
- **Parallel writable agents on a single working copy.** One working copy remains forbidden. Isolated-worktree parallelism is in scope through R3, with explicit resource claims.
- **Voting as proof.** Multiple opinions do not replace typed evidence, independent verification, or deterministic checks.
- **Unbounded recursive spawn.** The supervisor-worker design requires bounded, inspectable execution debt and finite escalation.
- **Automatic self-creation of roles.** Stable roles and their pins are configured data; adding roles requires an explicit maintainer decision and contract.

Acknowledging these limits is a feature of the roadmap, not a weakness in its wording. None of R1–R9 is claimed to be implemented here.
