---
name: model-routing
description: Route Grok Build work by stable job role and swappable model/effort pin.
when-to-use: which model, routing, conductor, implementation tier, review, security, explore
---

# Model routing

`glm-5.3-flash` via provider pool A is the conductor at the global default
`high` effort. It orchestrates and delegates; it does not map the repository
or implement delegated work. The conductor fallback chain is `glm-5.3-flash` →
`gemini-3.7-flash` → `grok-4.6`.
The configured advisory provider supplies a second opinion below 0.3 confidence
when its quota state is available; expired quota holds recover automatically. Web research stays on
`grok-4.6`. No open session can be hot-swapped.

Roles name jobs. Model and effort pins live only in `config/config.toml`:

| Role | Model @ reasoning effort |
| --- | --- |
| `explore` | `gpt-5.6-luna @ high` |
| `explore-thorough` | `gpt-5.6-luna @ max` |
| `implement-cheap` | `gpt-5.6-luna @ high` (effort-distinct from standard) |
| `implement-standard` / `implement` | `gpt-5.6-luna @ max` |
| `implement-hard` | `gpt-5.6-sol @ xhigh` |
| `implement-ops` | `glm-5.3 @ high` (provider pool A) |
| `implement-overflow` | `deepseek-v4-pro @ high` |
| `implement-cheap-fallback` | `deepseek-v4-flash @ high` |
| `plan-hard` / `plan` | `gpt-5.6-sol @ xhigh` |
| `review-hard` / `review` | `glm-5.3 @ max` |
| `review-independent` | `grok-4.6` |
| `expert-rescue` | `deepseek-v4-pro @ high` |
| `security` | `glm-5.3 @ max` (provider pool A) |
| `security-verify` | `grok-4.6` (official local OAuth; explicit availability signal required) |
| `visual-intake` | `minimax-m3` (explicit vision capability) |
| `visual-intake-deep` | `gpt-5.6-terra` (explicit vision capability) |

Provider pool A uses the configured `ZAI_API_KEY` credential pattern. Manual try-out candidates are tracked in the rebuild-stack Try-out pool and are never role-routable.
Visual intake is a TUI-owned pre-conductor preflight, not an intent or execution stage. The runtime must require `capabilities.vision = true`, validate schema v1, pass compact context plus opaque attachment IDs to the conductor, and use the deep role for high detail or re-analysis.
An explicit role default overrides the parent's/global effort. Unset role effort
means no role-level effort claim. Spawn by role name, never by model slug.

Every implementation, including low scope, starts with required delegated
reconnaissance: one linear `explore` for low complexity or the Stage C barrier
for medium/high complexity. Medium/high work always composes `review-panel` or
`review-hard`; unavailable review uses only the exact configured verifier
fallback. Failed spawns or bound-task retrievals reopen their stage and must be
repaired before bounded Stop enforcement releases execution debt. After the configured consecutive-failure threshold, a three-provider read-only consilium diagnoses the repair; an implementation tier applies the plan. `expert-rescue` diagnoses only.
The non-interactive batch pool uses `glm-5.3-flash` and `deepseek-v4-flash` for text, extraction, and classification. Never bind secrets in config.

## Refusal handling (act immediately: one honest reformulation or reroute; never mask)

- When a delegated model refuses a task on policy or safety grounds (cybersecurity refusals included), act immediately: never wait out retries and never replay the same prompt unchanged.
- Either reformulate once and redispatch to the same model, or reroute immediately to a role whose model is capable of and consents to the task class (for security-class work: the `security` pin or an approved consenting fallback). For security-class work, the reformulated prompt must begin with an explicit disclosure header stating this is an authorized security audit or Immunefi engagement (local-fork-only analysis where applicable); target identity may be withheld or pseudonymized, but the task class, authorization, and security purpose must stay explicit; reformulation may clarify or formalize the mathematics, but must not omit, downplay, or relabel the facts that identify the work as security analysis. If the model refuses the honest reformulation too, the refusal is terminal for that model/task pair: reroute and record both refusals (model, both prompt summaries, timestamps, chosen reroute) in the engagement evidence log (session evidence for non-bounty work).
- Every reformulation must preserve the task's true nature: neutral mathematical framing is permitted only with the security class still visible through the disclosure header. Never mask a security task as non-security to obtain compliance; the human-contractor litmus test applies: "would a human contractor receiving this exact wording understand they are doing security work?"
