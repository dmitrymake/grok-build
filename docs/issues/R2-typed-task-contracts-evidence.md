# R2 — Typed task contracts / evidence protocol

**Reference:** [`docs/ROADMAP.md#r2--typed-task-contracts--evidence-protocol`](../ROADMAP.md#r2--typed-task-contracts--evidence-protocol)

## Problem

Today a task id is completed by a terminal non-empty result, which is a stage attestation rather than independently verified evidence.

## Proposed shape

Define a typed `TaskSpec` with declared completion semantics and success criteria. Define a typed `TaskResult` with status, changed paths, artifacts, checks, criteria, and unresolved items. Independently verify paths and diff claims instead of taking child results on faith. Add the machine-checkable contract manifest already sketched for post-v0.1.0 hardening, and require specs and reviewers to cite it.

## Validation

Test successful, empty, malformed, wrong-path, and mismatched-diff results. Accept only evidence matching the task specification and manifest.

## Size

M

## Depends-on

None.
