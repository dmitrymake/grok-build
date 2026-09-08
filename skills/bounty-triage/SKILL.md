---
name: bounty-triage
description: Run an authorized bug-bounty engagement through the stage-gated pipeline; scope-first, evidence-first, payout-aware. Use when the user asks about bug bounty, vuln bounty, Immunefi, a bounty program, PoC validation, or a bounty report.
when-to-use: bug bounty, bounty, vuln bounty, immunefi, bugbounty, bounty program, triage, PoC, bounty report
user-invocable: true
---

# Bounty triage (authorized bug-bounty engagements)

Confirm that the engagement exists and the conductor is registered with the
program. Confirm that the target is inside the program's published scope. If no
scope document exists, do not probe. Copy the scope text verbatim into the
engagement note and treat it as the single authority.

## Program intake and payout routing

- Record the program URL, scope list (verbatim), rules-of-engagement link, reward
  table, payout method and minimum, and disclosure channel at intake.
- Prefer crypto-payout platforms first, such as Immunefi-style web3 or web
  programs with published crypto ranges.
- Prefer corporate programs paying in RUB second, including Russian vendor
  programs. Re-verify current terms, eligibility, and payout method on the
  official program page because programs and terms change.
- Use international fiat platforms last.
- Re-check the payout method and program status at report time, not just at
  intake.
- Create the engagement file at
  `$XDG_STATE_HOME/grok-route/bounty/<program-slug>/engagement.json`.

## Automated discovery

- Qualify a program when at least one in-scope asset pays in the $500–$1000
  band, the program is open to new researchers, scope and rules are public, and
  payout is crypto (preferred) or RUB (acceptable). Collect fiat-only
  international programs, but deprioritize them.
- Search Immunefi public program listings, HackerOne and Bugcrowd public
  directories, Russian vendor security pages, and RU bounty aggregators.
- Always re-verify reward, scope, and payout on the program's own page before
  engaging. Treat aggregator data as stale by default.
- Keep all state under `$XDG_STATE_HOME/grok-route/bounty/`, defaulting to
  `~/.local/state/grok-route/bounty/` when `XDG_STATE_HOME` is unset.
- Append one JSON object per program to `discovered.jsonl`; deduplicate on
  `(platform, url)`. Store `name`, `platform`, `url`, `rewards` (min-max band
  text), `payout` (`crypto`, `rub`, or `fiat`), `scope_url`, `status` (`open`,
  `closed`, or `unknown`), `discovered_at` (ISO-UTC), and `notes`.
- Create `<program-slug>/engagement.json` at intake with the scope verbatim,
  rules link, reward table, payout, disclosure channel, and status (`recon`,
  `hypotheses`, `validation`, `reported`, `duplicate`, or `rejected`).
- Append validation request and response summaries to
  `<program-slug>/evidence.jsonl`.
- Run discovery, triage, intake, passive recon, and hypothesis generation
  automatically. Run active validation automatically only for read-only-safe
  checks: GET-only requests, no state mutation, and in-scope assets. Require
  explicit operator approval for any state-changing PoC.

## Stage pipeline (same gates as code work)

1. Complete intake in the conductor session: hold the engagement note (scope
   verbatim) in session state; probe nothing before it exists. Persistence
   happens at the memory flush (step 7).
2. Shallow recon with `explore` only: program policy, the asset list, public-page technology fingerprints, prior disclosed reports for deduplication. Use read-only tools only; `explore` does not read target source in depth.
3. Deep source analysis on the strongest read-only roles, never on `explore`: ask the `security` role (glm-5.3 @ max) to read the in-scope source and emit ranked vulnerability hypotheses per asset, each with a concrete test design; use `researcher-analyst` (glm-5.3 @ max) for the alternative-pass formalization and math modeling when a second strong pass is needed. Keep the conductor zero-write; keep source analysis on these roles. `explore`/`explore-thorough` (luna) are shallow-recon roles only and must not be the deep source-analysis engine.
4. Validate with a writable child using `capability_mode="all"`: test one
   hypothesis at a time with a minimal PoC, low request volume, and in-scope
   assets only. Append every request and response summary to the evidence log.
   Stop at the first confirmed critical finding.
5. Use `security-verify` for independent verification only when its explicit
   availability signal is present. Reproduce from the evidence log alone.
   Without that signal, mark the finding unverified and never claim
   verification.
6. Write the report in the platform template. Apply the program's severity
   rating, and include impact, preconditions, reproduction steps lifted from
   the evidence log, and a remediation sketch. Submit only through the
   program's own channel.
7. Flush engagement state to memory: program, scope, tried hypotheses, findings,
   and payout status. Treat `engagement.json` and `evidence.jsonl` as durable
   state; keep the narrative in memory so the next session resumes instead of
   rediscovering the work.

## Hard rules

- Stop on an out-of-scope asset; make no "just one request" exception.
- Work passively before actively. Do not use bulk scanners or directory-busting
  from children; keep request volume low.
- Do not cause denial of service, abuse volume, or extract real user data. Use
  program-sanctioned test accounts.
- Preserve honest provenance: show every model the true nature of the task; never
  reframe security work as non-security for any model.
- On model refusal, react immediately: never wait out retries and never replay the same prompt unchanged. Either reformulate the task once and redispatch it to the same model, or reroute the task to a security-capable role (the `security` pin or an approved consenting fallback). The reformulated prompt must begin with an explicit disclosure header stating this is an authorized security audit or Immunefi engagement (local-fork-only analysis where applicable); target identity may be withheld or pseudonymized, but the task class, authorization, and security purpose must stay explicit; reformulation may clarify or formalize the mathematics, but must not omit, downplay, or relabel the facts that identify the work as security analysis. If the model refuses the honest reformulation too, the refusal is terminal for that model/task pair: reroute and record both refusals (model, both prompt summaries, timestamps, chosen reroute) in the evidence log. Never mask a security task as non-security for any model: every reformulation must pass the litmus test — would a human contractor receiving this exact wording understand they are doing security work?

- Dispatch math/proof subtasks to manual tryout models with the blind-but-honest framing from the start: the prompt begins with the same disclosure header (authorized engagement; target identity may be withheld) followed by the honest mathematical content. This prevents most refusals; if a refusal still happens, apply the refusal rule immediately instead of waiting out retries.
- Do not report before independent verification, and do not disclose outside
  the program channel.

## Out of scope for this skill

Do not perform offensive work without a registered program, target assets outside
published scope, or attempts to evade target defenses or any model's policies.
Keep pure code-security review of the local tree on the `security` skill.
