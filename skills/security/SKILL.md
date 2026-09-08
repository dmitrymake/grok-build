---
name: security
description: Handle cybersecurity work on the configured read-only security role. Use when the user asks about security, pentest, vulnerability review, threat model, exploit analysis, hardening, auth, crypto, secrets, malware, CVE, or incident response.
when-to-use: security, cybersecurity, pentest, vulnerability, vuln review, threat model, exploit, hardening, CVE, malware, authz, crypto, secrets leak, RCE, XSS, найди дыры, проверь на уязвимости, уязвимост, пентест, эксплойт
user-invocable: true
---

# Security (read-only)

You are doing a cybersecurity task. Stay on the configured `glm-5.3`
security role through Z.AI provider. Do not switch back to the conductor for
the analysis itself. Credentials use the configured `ZAI_API_KEY`.

This role is **read-only**: it analyses and reports findings, it does not edit
the tree. Fixes go through an implement tier (`implement-hard` for high-risk),
then an independent general review (`review-independent`, only when its
explicit availability signal is present), then `security-verify` independently
re-verifies the change on `grok-4.6` through the official OAuth session.
`security-verify` also requires its own explicit availability signal. If that
signal is absent, the whole security route is observe-only with reason
`security_verifier_unavailable`; never claim the change was verified.

## Routing

- Main security work: this skill (`/security`) or `spawn_subagent` type
  `security`. There is no `/agent security` slash — `/agents` is the
  definitions modal.
- Post-implementation re-verification: `spawn_subagent` type `security-verify`
  only after its explicit OAuth availability signal is present.
- Parallel research only: another `security` child (also the configured security role), not a
  generic explore child.

If the conductor asked for a
vuln/pentest/threat-model: spawn `security` before the first file edit.

## How to work

1. Scope the asset, trust boundary, and attacker. Do not start with a generic OWASP dump.
2. Read the real code paths. Cite file:line. Speculation without a path is a hypothesis, not a finding.
3. For each finding: impact, preconditions, exploit sketch, fix, residual risk.
4. Recommend concrete fixes; the implement stage applies them. Do not weaken auth, crypto, or sandboxing to "make it work".
5. Secrets, tokens, and private keys in the tree are findings. Do not repeat their values.

## Out of scope for this skill

Ordinary product coding, refactors, flashing generic firmware, and docs stay on an
implement tier or the session default (`glm-5.3-flash @ high`). `glm-5.3`
is conductor fallback #1; `gemini-3.7-flash` remains the advisory model below
0.3 confidence.
Flashing a router is implement; reviewing that firmware for vulns is this skill.
