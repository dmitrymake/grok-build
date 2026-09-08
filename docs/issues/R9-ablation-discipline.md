# R9 — Ablation discipline

**Reference:** [`docs/ROADMAP.md#r9--ablation-discipline`](../ROADMAP.md#r9--ablation-discipline)

## Problem

Shadow mode and telemetry show which stages run, but there is no cross-cutting discipline requiring stages to earn their continued place.

## Proposed shape

Use shadow mode and telemetry to delete stages that do not earn their place, not only to find bugs. Test removal of mandatory explore, `review-hard` on every medium route, the two-recon-role diversity, and consilium versus direct escalation. Treat retention or removal as evidence-based.

## Validation

Run the named ablations through R1, publish paired outcome and cost measurements, and retain a stage only when its measured contribution justifies its burden.

## Size

M

## Depends-on

R1.
