---
name: implement
description: Delegate implementation to the stable job role selected by the router.
when-to-use: implement, patch, fix tests, refactor, firmware
user-invocable: true
---

# Implement

Do not implement on the conductor. First finish the required read-only
reconnaissance: one `explore` job for low complexity,
or the composed Stage C barrier for medium/high work; use `explore-thorough`
only for delegated architecture-wide synthesis. Do not grep/list the tree on
the conductor while that recon is unfinished.

Use the router-selected job:

- `implement-cheap`, `implement-standard`/`implement`, `implement-hard` for the
  primary tiers, with model and effort pins resolved through config;
- `implement-ops` for firmware, destructive operations, or complex physical
  work;
- `implement-overflow` only after primary tiers fail;
- `implement-cheap-fallback` only as the final low-risk, explicitly testable,
  known-verifier writable fallback. Never use `explore` as an implementer.

Make the smallest change, follow repository conventions, and run relevant
tests or a safe dry-run. Medium/high work completes its composed review or the
exact configured verifier fallback before success. Escalate security review to
`security`, architecture to
`plan-hard`, and independent review to `review-independent`. Roles are job
names; model pins and reasoning efforts belong only in config.
