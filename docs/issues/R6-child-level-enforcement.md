# R6 — Child-level enforcement

**Reference:** [`docs/ROADMAP.md#r6--child-level-enforcement`](../ROADMAP.md#r6--child-level-enforcement)

## Problem

The subagent exemption prevents child deadlock but leaves writable children outside the per-action gate. The current cooperative-not-sandbox boundary does not enforce child effects.

## Proposed shape

Run writable children in ephemeral worktrees with writable-path allowlists, command and network policy, diff manifests, and a merge gate. Keep the cooperative limitation explicit while making permitted effects inspectable.

## Validation

Attempt allowed and disallowed writes, commands, network operations, and undeclared diffs. Require rejection or quarantine before merge.

## Size

L

## Depends-on

R2 and R3.
