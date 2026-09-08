# R4 — Loop detector

**Reference:** [`docs/ROADMAP.md#r4--loop-detector`](../ROADMAP.md#r4--loop-detector)

## Problem

Circuit breakers catch repeated same-role failures, but not A-to-B-to-A patch/revert cycles that produce no formal failure.

## Proposed shape

Fingerprint the task spec, changed paths, normalized diff, verifier failure, and delegation edge. Detect recurring fingerprints and patch/revert cycles, then emit typed block or escalation evidence instead of silently repeating work.

## Validation

Replay synthetic patch/revert, same-change, and legitimate iterative-fix traces. Detect loops without blocking monotonic progress.

## Size

M

## Depends-on

R2.
