# R5 — Budget hooks

**Reference:** [`docs/ROADMAP.md#r5--budget-hooks`](../ROADMAP.md#r5--budget-hooks)

## Problem

The runtime tracks stages and circuit state, but has no general token, cost, or spawn budget gate for retries, review, and overflow.

## Proposed shape

Add token, cost, and spawn counters with configured limits and a `budget.exceeded` gate. Preserve which counter exceeded and prevent further work unless a bounded policy explicitly permits it.

## Validation

Exercise zero, boundary, and over-budget fixtures, including retries and barriers. Verify stable gate behavior and telemetry reasons.

## Size

S/M

## Depends-on

R2.
