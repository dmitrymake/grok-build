# R1 live evaluation protocol

This protocol is manual and mirrors the H1 sandbox procedure. It does not automate live orchestration.

## Preparation

1. Create a throwaway `HOME` and an isolated task worktree from the locked repository snapshot.
2. Install the tree with `HOME=<throwaway-home> ./scripts/install.sh`.
3. Supply only pre-existing operator authentication through explicitly selected symlinks. Do not copy credentials into the task worktree.
4. Freeze the fixture, rubric, acceptance digest, variant, controls, trial ordering, pairing ID, and run-record schema before starting.

## Execution and isolation

The operator starts the selected variant interactively in the isolated worktree. The tested run can see the public fixture and ordinary run outcome, but not controller verifier source, hidden assertions, or detailed verifier failures. The run is allowed to terminate before grading. After termination, the controller runs the hidden verifier outside the sandbox against the final artifact, then records only its result and timing in the run record. The controller retains source and detailed failures outside the tested run and repository.

The operator records who initialized the locked record, who ran the task, and who performed post-run adjudication. The run record records model, provider, effort, permissions, timestamps, telemetry references, usage, final diff, verifier result, and invalid-run evidence. The controller owns fixture freeze and verifier fields; the operator owns observed run and telemetry fields; the adjudicator owns acceptance and hidden-verifier outcomes.

## Trial count and uncertainty

Use a minimum of 10 valid trials per task and variant for an initial directional result; use 20 or more when the expected difference is small. Report valid and invalid trials separately, and report missing data rather than imputing it. For paired cells, preserve pairing IDs and compute paired deltas only when all locked controls match. Use a bootstrap confidence interval over paired trial deltas (at least 5,000 resamples), with the random seed recorded. Repeat the trial sequence with a fresh ordering seed when results are near the decision boundary; report the number of repetitions and the interval, not only a point estimate.

An environment or setup failure is INVALID, never a scored failure or a silently dropped denominator. Unknown adjudication, unavailable verifier results, incomplete telemetry, and missing token usage remain explicit missing data according to the run-record metric semantics.

## Sequencing and report arms

The first one or two comparisons should be the configured-width versus width-minus-one recon rows, followed by same-lens versus diverse-lens. These R9 comparisons provide the maximum signal per manual run: they isolate profile width and lens diversity before broader pipeline costs obscure the effect.

The report includes:

- full-pipeline and every declared ablation;
- single best agent in its **NATIVE harness**, explicitly marked **UNPAIRED** unless all locked controls match (the controlled single-best-agent baseline remains separate);
- an open-weight-only configuration;
- matched-budget framing for every comparison, including task, initial state, permissions, trial budget, and verifier timing.

The controlled `single-best-agent` arm is one tested agent with no recon, planning, review, or auxiliary model stages. Native-harness results are not substituted for that baseline.

## Judge health and hidden holdouts

Judge evaluation is opt-in and controller-owned. The candidate, proposer, applier, and semantic judges receive no holdout path, reference solution, verifier source, or answer key. The controller resolves the holdout root outside the repository, rejects symlink or realpath escapes, verifies the frozen digest, disables network access, and mounts holdout data read-only for the grader. If any boundary cannot be demonstrated, the run records `boundary_unverified` and is not scored. Only aggregate PASS/FAIL/ABSTAIN outcomes and opaque evidence references may enter run artifacts. The frozen canary battery runs after the agent run; repeated 100% success is recorded as `contamination_suspected`, not as perfect judge health.

## Non-goals

R1 does not provide automated live orchestration, a summarizer, public benchmark integration, or adaptive topology. Adaptive topology is reserved for R7 and is accepted by the offline namespace only as non-executable in this iteration.
