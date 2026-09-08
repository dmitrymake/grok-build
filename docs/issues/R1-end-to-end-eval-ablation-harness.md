# R1 — End-to-end eval + ablation harness

**Reference:** [`docs/ROADMAP.md#r1--end-to-end-eval--ablation-harness`](../ROADMAP.md#r1--end-to-end-eval--ablation-harness)

## Problem

The 50-case corpus proves that the router builds the expected pipeline, not that the pipeline solves tasks better.

## Proposed shape

Build a repeatable matrix for a single-best-agent baseline, the full pipeline, and ablations: no recon, no LLM review, deterministic-verifier-only, same-provider versus cross-provider review, and fixed versus adaptive topology. Measure task success, hidden-test success, false-success rate, false-BLOCKED rate, tokens, wall-clock time, unnecessary-agent rate, and N trials per task. Measure environment changes rather than final-text diffs.

## Validation

Run the harness on at least one real task where at least one stage proves measurably load-bearing or redundant. This issue validates itself through that result.

## Size

L

## Depends-on

None.
