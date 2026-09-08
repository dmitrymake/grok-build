# R8 — Neutral event ABI + adapter split

**Reference:** [`docs/ROADMAP.md#r8--neutral-event-abi--adapter-split`](../ROADMAP.md#r8--neutral-event-abi--adapter-split)

## Problem

The v0.1.0 implementation is extracted around the Grok hook contract, so its lifecycle and integration boundary are host-specific.

## Proposed shape

For a v1.0 architecture, separate `multiagent-core` (events, decisions, execution graphs, state, and policies) from adapters for `grok`, `claude_code`, `codex`, and `opencode`. Define neutral events: `PromptAccepted`, `StageScheduled`, `ChildSpawnRequested`, `ChildTaskBound`, `ChildCompleted`, `ChildFailed`, `VerifierStarted`, `VerifierPassed`, `VerifierFailed`, `RunBlocked`, and `RunCompleted`.

## Validation

Implement one host adapter and replay equivalent traces. Verify host-independent core decisions and state transitions while keeping adapter capabilities explicit.

## Size

XL

## Depends-on

R2, R3, R5, and R6.
