---
name: rebuild-stack
description: Re-pin stable Grok Build job roles when a genuinely better model appears.
when-to-use: new model, rebuild stack, refresh stack, update combine
user-invocable: true
---

# Rebuild the Grok Build combine

Try-outs live outside the stable pins and are listed in the Try-out pool section. Run `/corpus-sync` before any re-pin: a fresh workload corpus is the A/B track that makes tier and role changes measurable.

Roles are stable jobs. Models are consumable pins in `config.toml`. Never create
or rename a role after a vendor, provider, operator contract, or model family.

Current pins are not maintained in this skill. Inspect the live configuration with `grok-route roles`; pins live only in `config/config.toml`.

Use `grok-route models sync` followed by `grok-route models diff` as the standard source for what appeared upstream before considering a re-pin. The standard `reasoning_effort` is explicit for non-Grok, non-visual roles; Grok roles inherit the configured default, while visual roles use their own mode.

Provider pool A uses the configured `ZAI_API_KEY` credential pattern.
Only re-pin a role when the candidate is reachable through an approved provider
and clearly improves that job. The configured advisory provider supplies the
conditional fallback and second opinion. Update `config.toml`, matching agent descriptions,
routing tests, this skill, model-routing, the routing rule, and docs. Preserve
role identifiers and aliases. Consilium pins may swap models but must retain three distinct providers. Do not add raw OAuth/API credentials, a
`[model."grok-4.6"]` binding, or a new provider merely because it is new.

For a visual-role re-pin, verify explicit catalog vision capability, schema-v1 compatibility, fallback independence, and cache invalidation through `binding_version`; never infer capability from a model id. Run both Grok test suites and report role, old pin → new pin, payment pipe, files changed, and refused additions.

## Try-out pool

There are two tracks: proven role pins require the clear-improvement bar; try-out additions require only hands-on promise.
Try-outs are manual `/model` candidates only: never bind them to `[subagents.roles.*]`, `[subagents.models]`, intents, or any routing pin. Admission requires reachability through an approved configured provider, a config `[model."id"]` block, and a `providers.json` catalog entry with `tier: "tryout"`. The pool is capped at 3 concurrent try-outs, and each entry is dated.

Every `/rebuild-stack` run re-reviews the pool: keep, promote only through the normal stable-role admission policy including A/B and calibration evidence, or retire when unavailable, superseded, or drained by quota without a known near-term recovery. A provider quota hold with a scheduled reset or auto-expiry defers hands-on evaluation; it is not a retirement trigger. Retirement means removing the config block, catalog entry, and pool row.

Record provenance dated like existing probe notes; never record credentials.

| Model | Pipe | Added | Notes |
| --- | --- | --- | --- |
| gpt-6-astra | codex | 2026-09-04 | Frontier OpenAI model on the ChatGPT-subscription codex pipe; backend-verified 2026-09-04; hands-on via /model before any role pin. |

Provider quota holds auto-expire. The non-interactive batch pool handles text, extraction, and classification.

After rotating pins, mirror the same pins into `config/config.example.toml` and regenerate the contract prose in the same session, or the generated contract and sample config drift from the live pins.
