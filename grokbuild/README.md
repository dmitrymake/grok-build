# Grok Build intent router

Execution tracks persist `requested_at` for parallel members. A fully requested and task-bound barrier is lifted to observe-only after the 30-minute watchdog threshold and records `barrier_stall`; completion and failure state are unchanged. Task termination is allowed as the sole lifecycle exception. Read-only pipelines/chains are quote-aware and allow `--flag=value` while rejecting redirects, interpreters, substitutions, assignments, unknown commands, and per-command write-capable flags such as output targets or mount/date mutation options. Generated station recipes are NEXT-first; `[subagents.models].general-purpose` pins the built-in infrastructure role.

## Contract

This directory implements deterministic intent classification, executor
selection, stage tracking, policy gates, and redacted telemetry. It does not
switch the model of an open session and does not call models itself.

Two fixed Grok hook constraints shape this design: there is no hook event that
switches the session model before the first token, and `spawn_subagent` is
resolved by agent type, never a model slug. `UserPromptSubmit` stdout is
ignored by the harness, so the passive gate (`PreToolUse` deny plus `Stop`
block) is deliberate, not a limitation to route around. The hook reads the
effective installed config, whose repository source of truth is
`config/config.toml`; deterministic tests pass the repository config path
explicitly, so no live config or paid API is consulted and drift stays diagnosable.

<!-- grok-build:generated begin id=routing-contract schema=1 -->
The parent chain is `glm-5.3-flash → gemini-3.7-flash → grok-4.6`; stable role names, not model slugs, are delegated.

Source ownership: `config/config.toml` owns role/model/effort pins and workspace verifiers; `providers.json` owns provider, tier, auth, and capabilities.

### Tier mapping
- **recon:** `explore` = `gpt-5.6-luna @ high [standard]`, `explore-thorough` = `gpt-5.6-luna @ max [standard]`
- **research:** `researcher` = `gpt-5.6-sol @ max [broad]`, `researcher-analyst` = `glm-5.3 @ max [broad]`, `researcher-challenger` = `grok-4.6 [standard]`
- **cheap:** `implement-cheap` = `gpt-5.6-luna @ high [guided]`
- **standard:** `implement-standard` = `gpt-5.6-luna @ max [guided]`, `implement` = `gpt-5.6-luna @ max [guided]`
- **strong:** `implement-strong` = `gpt-5.6-sol @ max [standard]`
- **hard:** `implement-hard` = `gpt-6-astra @ max [broad]`
- **ops:** `implement-ops` = `glm-5.3 @ high [standard]`
- **overflow:** `implement-overflow` = `deepseek-v4-pro @ high [standard]`
- **fallback:** `implement-cheap-fallback` = `deepseek-v4-flash @ high [guided]`
- **plan:** `plan-hard` = `gpt-6-astra @ max [broad]`, `plan` = `gpt-6-astra @ max [broad]`, `planner-strong` = `gpt-5.6-sol @ max [standard]`
- **review:** `review-hard` = `glm-5.3 @ max [standard]`, `review` = `gpt-5.6-sol @ xhigh [standard]`
- **consilium:** `consilium-analyst` = `glm-5.3 @ max [broad]`, `consilium-challenger` = `deepseek-v4-pro @ high [standard]`, `consilium-arbiter` = `gpt-5.6-sol @ xhigh [standard]`
- **security:** `security` = `glm-5.3 @ max [standard]`
- **visual:** `visual-intake` = `minimax-m3 [standard]`, `visual-intake-deep` = `gpt-5.6-terra [standard]`

Escalation is cheap → standard → hard. Overflow is used only after primary tiers fail. Flash is a strict last resort for low-risk, testable work with a known verifier. Grok roles require an explicit availability signal; provider pool A roles use `glm-5.3` with `ZAI_API_KEY`.
<!-- grok-build:generated end id=routing-contract schema=1 -->

## Layout

| Path | Purpose |
| --- | --- |
| `intents.json` | intent anchors and workspace verifiers; no model pins |
| `profiles.json` | policy modes, weights, thresholds, and circuit settings |
| `providers.json` | provider pool, capability tier, and auth metadata |
| `features.py`, `classify.py` | trusted-user extraction, features, overrides, scoring |
| `roles.py` | validated role registry layered from config and provider metadata |
| `pipeline.py` | tier selection and linear/parallel execution composition |
| `decision.py` | typed schema-v4 decision and conductor handoff |
| `policy.py`, `router.py` | policy resolution and routing facade |
| `state.py`, `cache.py` | transactional runtime state, execution tracks, and route cache |
| `payloads.py` | task-tool payload parsing, retrieval synthesis, and transcript fallback seams |
| `visual_intake.py`, `visual_cache.py` | schema-v1 visual preflight coordinator and private bounded result cache |
| `conductor.py` | pre-session cached-state/credential fallback resolver |
| `stats.py`, `redact.py` | generation-1 metrics and secret-safe logging |
| `discovery.py` | upstream model/quota discovery into an advisory cache; never mutates pins or availability |
| `cli.py` | explain/state/provider/replay/stats/roles/profiles/conductor/models/contract-check/version |

Visual intake runs before the conductor only when the TUI supplies normalized image attachment references. It is not represented in `intents.json` or executor pipelines. Capability eligibility is explicit catalog metadata. Validated results become compact auxiliary context; original bytes and paths never enter this package. Failures become typed markers, and `request_visual_analysis` reuses the deep coordinator path.

Runtime files:

- `~/.cache/grok/visual-intake.json` — schema/binding/options/hash-keyed validated visual results, mode 0600, 24-hour TTL.
- `~/.cache/grok/route.json` — per-session/turn/prompt decision cache, mode 0600.
- `~/.cache/grok/models.json` — advisory upstream model/quota cache from
  explicit `models sync` runs, mode 0600; failures keep the last-good data.
- `~/.local/state/grok-route/state.json` — transactional state, mode 0600.
- `~/.local/state/grok-route/route.jsonl` — append-only redacted log; rotated
  chronologically as `.2`, `.1`, current.
- `~/.local/state/grok-route/payload-debug.enabled` — explicit opt-in marker for raw task-payload diagnostics.
- `~/.local/state/grok-route/payload-debug.jsonl` — raw diagnostic captures written only while the marker exists. `grok-route contract-check` validates the latest retrieval and optional background-terminal capture structurally (keys and JSON types), ignoring values; use `--capture PATH` for a deterministic capture file.

## Decision engine

`router.route_prompt(...)` returns `RouteDecision` schema v4, telemetry
generation 1. A decision contains identity and policy ids, repo/session/turn,
task class, complexity, risk, extracted features, selected `(role, model,
effort)`, warnings/fallbacks, write policy, and an execution pipeline.

Prompt scoring merges two views of the real user text:

- stripped: removes harness `<system-reminder>` and `<user_info>` blocks;
- raw trusted-user: keeps their content while unwrapping only an outer
  `<user_query>`.

The conservative merge prevents a fake embedded reminder from hiding a strong
intent. Synthetic turns, feedback injections, and pure harness reminders are
excluded before scoring. Strong anchors beat phrase-only matches; profile
priority breaks ties. Explicit intent overrides are `route=security:`,
`route: security`, `--route security`, and `@security`. Modifiers
`route=cheap|fast|quality` express preference only and cannot remove mandatory
security/review stages.

Unmatched prompts are a deliberate observe-only policy, not a gap: the decision
keeps `intent=None` with reason `no_intent` (nothing scored) or
`below_threshold` (score under the profile floor), an empty execution
pipeline, `write_policy=observe`, no edit/stop gates, and no second opinion.

### Executor pipelines

- **Implement low:** `explore` → selected implementation → ordered exact verifiers (`verify/0`, then `verify/1` in the repository checkout).
- **Implement medium:** a profile-driven parallel recon barrier (default width
  three) → implementation → `review-hard` → ordered verifiers. Lens roles and
  reasons come from `barrier_lenses.json`.
- **Implement high:** the profile-driven recon barrier adds two members (default
  width five); optional `plan-hard` precedes implementation. High-risk review is
  a fixed two-member keyed barrier of `review-hard` plus `review-independent`
  when the independent role has an explicit safe availability signal; otherwise
  it degrades to linear `review-hard` with a warning. `review_barrier_width` defaults to 1 (unchanged single reviewer); the production catalog currently yields at most two family-diverse reviewers, so insufficient diversity degrades with a warning.
- **Research:** a profile-driven barrier defaults to three members. High research
  adds two members, including `researcher-challenger` only with its verified
  availability signal; without it, the next eligible lens fills the width.
- **Security:** provider pool A `security` analysis → implementation → optional independent
  review → Grok `security-verify` → ordered verifiers. Each verifier requires its own
  explicit availability signal; without it the whole route is observe-only with
  reason `security_verifier_unavailable` and must never be called verified.
- **Direct review:** exactly one read-only `review-hard`; no implementation,
  verifier, planning, or independent review is added.
- **Plan/ADR:** required `plan-hard`; writing the requested artifact still
  belongs to a writable child. Entering `/plan` is not completion evidence.
- **Explore:** required read-only `explore`; edits and Stop remain gated.
- **Research:** staged read-only barrier with `researcher` and `researcher-analyst`; high-complexity investigations also require `researcher-challenger`.

Parallel participants are tracked by stable member keys, so two `explore`
instances cannot collapse into one completion. A background acknowledgement
only binds its task id. Completion requires terminal non-empty text retrieved
for that exact id; running, queued, empty, structured, unknown-id, and timeout
results do not complete a member or reset its circuit breaker. Typed `not_found`
results likewise never complete a member; after two observations they release
the binding with cause `task_not_found` and retain a tombstone for late
correlation.

### Deterministic verifier

`kind="verify"` is non-spawnable and tracked separately from child stages. It
is added only when the workspace entry in `intents.json` declares executable
verifiers. The repository checkout declares two ordered stages: `verify/0` runs
`./tests/grok-route-test.sh`, then `verify/1` runs
`./tests/grok-combine-install-test.sh`. The gate permits only the currently due
exact `shlex` argv; both stages must succeed: no wrappers, extra
arguments, redirects, pipelines, or metacharacters.

## Hook state machine

The hook observes and enforces cooperation; it never invokes a tool or model.

1. `UserPromptSubmit` classifies and persists without stdout.
2. `PreToolUse` validates that a spawn matches the next linear stage or an
   outstanding member of the current parallel barrier.
3. While a required stage is pending, edit-capable tools are denied. During
   recon, broad conductor mapping (`grep`, `list_dir`, search/web search, and
   shell `ls/find/rg/grep`) is also denied; narrow `read_file` remains available.
4. `PostToolUse` records a valid terminal child result. Background acknowledgements bind
   both parallel members (`member_tasks`) and linear stages (`stage_tasks`), while
   retrieval completes the matching stage only from a terminal result. `PostToolUseFailure`
   marks failure and transactionally feeds the member role's circuit breaker.
5. `Stop` blocks once with the exact next recipe or an explicit hold.
6. Stage Gating feedback prints `member-key=task-id` for bound members awaiting
   retrieval, allowing the conductor to recover after a lost chat id.
7. Members without a persisted task id remain unbound and retryable; their
   feedback continues to print only the member key.
8. Positively identified `subagent` and `subagent_resume` sessions are exempt,
   preventing child deadlock. The conductor stays zero-write even after gates
   complete; only children implement.

Unspawnable, unavailable, failed, or circuit-open required roles degrade to
observe-only instead of an impossible permanent hold. Phrase-only implement is
profile-gated; phrase-only security remains observe-only without a strong
anchor. `run_terminal_command` parsing is a cooperative routing heuristic, not
a sandbox.

#### Testing

`test_workloads.py` runs the 70-case recurring-work corpus in
`fixtures/workloads.json` against static routing and checks intent, selected
role, and review-stage invariants. Add a case with a stable id and expected
contract when a recurring workload or routing boundary changes; run the script
through its `main()` entry point before updating expectations.

`corpus_sync.py import-history` classifies redacted Grok and Claude history by
provenance before staging. Automation is never staged. Uncertain prompts need a
local commit within 48 hours or explicit `--keep-uncertain`; audit fields retain
the matched rule and git evidence. `import-git` mines local, non-chore commit subjects as the high-precision corpus
backbone without contacting remotes. `harvest` recovers redacted prompts from local telemetry into
`~/.local/state/grok-route/workloads-staging.json`; `triage` prints a priority-
sorted review batch, and `status` reports watermark and counts. The staging
cap is 500 entries, ranked drift > negative-candidate > git-derived >
uncertain-origin+git > history-import > near-dup; lowest-rank oldest entries
are evicted and protected by bounded `rejected_hashes`. History uses only a
bounded prompt-hash set plus a size/mtime unchanged-file skip; changed,
rotated, and replaced files are fully rescanned. Edit `expect` manually in the
state-directory staging file, then run `merge`; observed routing never
adjudicates expectations. Run both canonical test suites afterward.

### Modes

| Mode | Classify | Persist | Deny/block |
| --- | --- | --- | --- |
| `static` | yes | no | no |
| `shadow` | yes | yes | no |
| `dynamic` | yes | yes | yes |

*The permanent conductor zero-write deny for edit-capable tools and write shell
commands applies in every mode; only stage-gate denies are lifted in `static`
and `shadow`.

Resolution order is `GROK_ROUTE_MODE`, then `GROK_ROUTE_ENFORCE=1`, then
`GROK_ROUTE_SOFT=0`, then profile default. `shadow` is the urgent observe-only
escape hatch. Existing sessions must reload hooks or restart after env changes.

## Model/effort recipes

An explicit role effort in `config/config.toml` overrides parent/global effort;
an omitted role effort is unset at the role layer. Recipes render
`model @ effort` only when effort is explicit, otherwise just `model`.
Compatibility `[subagents.models]` entries do not override a role spec. Alias
pairs are validated for exact model/effort equality.

Every distinguishable role must have a distinguishable effect — at minimum via
reasoning effort, lens, or pipeline position; a role whose selection changes
nothing is an uncovered branch.

## Telemetry and reason codes

Every real submit is persisted, including no-intent decisions. There is exactly
one `pre_tool_use_outcome` record per PreToolUse and one Stop record per Stop.
Records contain stable reason codes and routing identity but never prompt text.
Residual secret patterns are redacted.

Reason-code families cover:

- exemption/inactive/non-dynamic gates;
- allowed spawn or parallel member;
- wrong linear stage or wrong barrier member;
- pending or exact verifier;
- zero-write, unknown-write, and recon-diet denies;
- allowed reads/unmatched tools and lifted gates.

`stats` accepts schema v4 / generation 1 only, reports malformed/skipped input,
and computes deduplicated intent-routing, pair divergence, selected-pair
distribution, legacy model divergence, tier/fallback divergence,
overall/gate/zero-write deny rates, Stop blocks, repeated-deny friction, and
optional label coverage/FPR. Pair divergence compares the routed effective pair
`(model, explicit effort or configured default_reasoning_effort)` with the
configured default pair over routed submits; it is `null` when config is
unavailable. Distribution rows preserve raw effort and render null as
`inherit`. `model_divergence` is deprecated and retained for one telemetry
generation. See
[`docs/grok-build.md`](../docs/grok-build.md) for metric definitions and
runbooks.

## CLI and verification

```bash
python3 -m grokbuild.cli explain "find RCE in demo-api" --json
python3 -m grokbuild.cli state
python3 -m grokbuild.cli replay --last 20
python3 -m grokbuild.cli stats --json
python3 -m grokbuild.cli roles
python3 -m grokbuild.cli profiles
python3 -m grokbuild.cli conductor
python3 -m grokbuild.cli conductor --model-only
python3 -m grokbuild.cli models sync
python3 -m grokbuild.cli models diff
python3 -m grokbuild.cli models quota

./tests/grok-route-test.sh
./tests/grok-combine-install-test.sh
```

For installation, provider/auth bindings, context limits, telemetry paths, and
operator runbooks, use the current-state
[`docs/grok-build.md`](../docs/grok-build.md).
