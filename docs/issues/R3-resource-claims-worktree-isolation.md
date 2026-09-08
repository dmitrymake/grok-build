# R3 — ResourceClaim + worktree isolation

**Reference:** [`docs/ROADMAP.md#r3--resourceclaim--worktree-isolation`](../ROADMAP.md#r3--resourceclaim--worktree-isolation)

## Problem

A barrier member key identifies a participant but does not establish file ownership. Parallel writable agents could contend for resources.

## Proposed shape

Add `ResourceClaim` with `owner_task_id`, resource glob, `exclusive-write`, and lease. Parallel writable work on one working copy remains forbidden. Support writable parallelism only across isolated worktrees, with claims and lease handling governing ownership.

## Validation

Test overlapping and disjoint claims, lease expiry and renewal, rejection of one-working-copy parallel writes, and attributable diffs when isolated worktrees are merged.

## Size

L

## Depends-on

R2.
