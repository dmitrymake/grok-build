# Security Policy

## Supported versions

Only the latest version on the default branch is supported with security fixes.
Older releases may not receive updates.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's **Report a
vulnerability** flow in the repository's Security tab (GitHub private
vulnerability reporting). Do not open a public issue or discussion for an
unfixed vulnerability. If that flow is unavailable, contact the repository
owner through GitHub and request a private channel; no email address is
provided here.

Please include enough detail to reproduce the issue, its impact, affected
version or commit, and any suggested mitigation. Please do not include real
credentials or other sensitive data.

Please allow time for investigation and coordinated disclosure. Do not make a
vulnerability public until a fix has shipped or the repository owner has
agreed that disclosure is appropriate.

This is a personal project, so responses and fixes are best-effort rather than
covered by a guaranteed response-time SLA.

## Scope and limitations

Grok Build is a cooperative routing and stage-gating layer for the Grok CLI.
Its cooperative model is not a sandbox: do not run untrusted prompts through it
expecting process or filesystem isolation. The security model and limitations
are documented in [`docs/architecture.md`](docs/architecture.md) and the
project README.
