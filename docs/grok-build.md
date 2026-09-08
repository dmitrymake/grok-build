# Grok Build combine

<!-- grok-build:generated begin id=runbook-contract schema=1 -->
Grok Build uses stable job roles with model pins and efforts configured in `config/config.toml`.

The non-spawnable conductor follows `glm-5.3-flash → gemini-3.7-flash → grok-4.6`, starts with `glm-5.3-flash @ high`, and remains permanently zero-write.

| Role | Model @ effort | Autonomy | Capability | Model tier | Context | Provider | Subscription class | Endpoint/auth |
| --- | --- | --- | --- | --- | ---: | --- | --- | --- |
| conductor (not spawnable) | `glm-5.3-flash @ high` | standard | zero-write | conductor | 1M | zai | primary | zai; `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `consilium-analyst` | `glm-5.3 @ max [broad]` | `broad` | read-only | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `consilium-arbiter` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `consilium-challenger` | `deepseek-v4-pro @ high [standard]` | `standard` | read-only | overflow | 1M | opencode | primary | `https://opencode.ai/zen/go/v1`; api_key; env OPENCODE_GO_API_KEY |
| `criterion-judge` | `glm-5.3-flash @ high [standard]` | `standard` | read-only, vision | flash | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `expert-rescue` | `deepseek-v4-pro @ high [standard]` | `standard` | read-only | overflow | 1M | opencode | primary | `https://opencode.ai/zen/go/v1`; api_key; env OPENCODE_GO_API_KEY |
| `explore` | `gpt-5.6-luna @ high [standard]` | `standard` | read-only | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `explore-risk` | `minimax-m3 @ high [standard]` | `standard` | read-only, vision | standard | 1M | minimax | primary | `https://api.minimax.io/v1`; api_key; env MINIMAX_API_KEY |
| `explore-thorough` | `gpt-5.6-luna @ max [standard]` | `standard` | read-only | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `frontier-resolver` | `grok-4.6 [standard]` | `standard` | read-only | spare | session-managed | xai | primary | ``; official grok login; session; no binding |
| `frontier-resolver-standby` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `implement` | `gpt-5.6-luna @ max [guided]` | `guided` | all | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `implement-cheap` | `gpt-5.6-luna @ high [guided]` | `guided` | all | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `implement-cheap-fallback` | `deepseek-v4-flash @ high [guided]` | `guided` | all | cheap | 1M | opencode | primary | `https://opencode.ai/zen/go/v1`; api_key; env OPENCODE_GO_API_KEY |
| `implement-hard` | `gpt-6-astra @ max [broad]` | `broad` | all, vision | frontier | 1 050 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `implement-ops` | `glm-5.3 @ high [standard]` | `standard` | all | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `implement-overflow` | `deepseek-v4-pro @ high [standard]` | `standard` | all | overflow | 1M | opencode | primary | `https://opencode.ai/zen/go/v1`; api_key; env OPENCODE_GO_API_KEY |
| `implement-standard` | `gpt-5.6-luna @ max [guided]` | `guided` | all | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `implement-strong` | `gpt-5.6-sol @ max [standard]` | `standard` | all | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `judge-challenger-agentic` | `gpt-oss-120b @ low [standard]` | `standard` | read-only | challenger | 131 072 | together | primary | `https://api.together.xyz/v1`; api_key; env TOGETHER_API_KEY |
| `judge-challenger-structural` | `gpt-oss-120b @ low [standard]` | `standard` | read-only | challenger | 131 072 | together | primary | `https://api.together.xyz/v1`; api_key; env TOGETHER_API_KEY |
| `judge-disagreement` | `gpt-5.6-luna @ high [standard]` | `standard` | read-only | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `judge-frontier-code` | `grok-4.6 [standard]` | `standard` | read-only | spare | session-managed | xai | primary | ``; official grok login; session; no binding |
| `judge-frontier-general` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `judge-independent` | `glm-5.3-flash @ high [standard]` | `standard` | read-only, vision | flash | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `judge-primary` | `gemini-3.7-flash @ high [standard]` | `standard` | read-only | conductor | 1 048 576 | commandcode | reserve | `https://api.commandcode.ai/provider/v1`; api_key; env COMMANDCODE_API_KEY |
| `plan` | `gpt-6-astra @ max [broad]` | `broad` | read-only, vision | frontier | 1 050 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `plan-comparator` | `gemini-3.7-flash @ high [standard]` | `standard` | read-only | conductor | 1 048 576 | commandcode | reserve | `https://api.commandcode.ai/provider/v1`; api_key; env COMMANDCODE_API_KEY |
| `plan-hard` | `gpt-6-astra @ max [broad]` | `broad` | read-only, vision | frontier | 1 050 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `planner-a` | `deepseek-v4-pro @ high [standard]` | `standard` | read-only | overflow | 1M | opencode | primary | `https://opencode.ai/zen/go/v1`; api_key; env OPENCODE_GO_API_KEY |
| `planner-b` | `glm-5.3 @ high [broad]` | `broad` | read-only | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `planner-c` | `gpt-5.6-luna @ high [standard]` | `standard` | read-only | cheap | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `planner-strong` | `gpt-5.6-sol @ max [standard]` | `standard` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `researcher` | `gpt-5.6-sol @ max [broad]` | `broad` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `researcher-analyst` | `glm-5.3 @ max [broad]` | `broad` | read-only | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `researcher-challenger` | `grok-4.6 [standard]` | `standard` | read-only | spare | session-managed | xai | primary | ``; official grok login; session; no binding |
| `review` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | read-only | hard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |
| `review-hard` | `glm-5.3 @ max [standard]` | `standard` | read-only | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `review-independent` | `grok-4.6 [standard]` | `standard` | read-only | spare | session-managed | xai | primary | ``; official grok login; session; no binding |
| `security` | `glm-5.3 @ max [standard]` | `standard` | read-only | conductor | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `security-verify` | `grok-4.6 [standard]` | `standard` | read-only | spare | session-managed | xai | primary | ``; official grok login; session; no binding |
| `verifier-planner` | `glm-5.3-flash @ high [standard]` | `standard` | read-only, vision | flash | 1M | zai | primary | `https://api.z.ai/api/coding/paas/v4`; api_key; env ZAI_API_KEY |
| `visual-intake` | `minimax-m3 [standard]` | `standard` | read-only, vision | standard | 1M | minimax | primary | `https://api.minimax.io/v1`; api_key; env MINIMAX_API_KEY |
| `visual-intake-deep` | `gpt-5.6-terra [standard]` | `standard` | read-only, vision | standard | 272 000 | codex | primary | `http://127.0.0.1:1456/v1`; configured proxy endpoint and API key |

After `consilium_after_failures` consecutive failures in one repair stage, the harness convenes a read-only, three-provider consilium; an implementation tier applies the arbiter's plan.

Provider pool A roles use `glm-5.3` via `ZAI_API_KEY`.
Proxy-backed models use a configured proxy endpoint and API key.
Web research runs on `grok-4.6`. Batch models `glm-5.3-flash` and `deepseek-v4-flash` handle text, extraction, and classification workloads.
<!-- grok-build:generated end id=runbook-contract schema=1 -->

## Installation and configuration

Install or refresh the local command and hooks from the repository checkout:

```bash
./scripts/install.sh
python3 -m grokbuild.cli roles
```

`config/config.toml` is the source of truth for model, role, effort, and verifier pins. Provider metadata and capabilities live in `grokbuild/providers.json`; intent rules and workspace verifier declarations live in `grokbuild/intents.json`.

The repository launcher exposes the CLI directly (the installer does not place a `grok-route` executable in `PATH`):

```bash
./scripts/grok-route explain "review this change" --json
python3 -m grokbuild.cli state
python3 -m grokbuild.cli roles
python3 -m grokbuild.cli profiles
python3 -m grokbuild.cli conductor
```

## Visual intake

The TUI owns image bytes and invokes the pre-conductor coordinator. This repository provides the schema-v1 validator, policy, resolver, cache, and callable invoker contract. Image turns follow normalize → `visual-intake` → validation/cache → compact visual context → conductor. Text-only routing is unchanged. Attachments are represented only by opaque IDs and SHA-256 hashes; image bytes do not enter conductor context.

`visual_intake.mode = "auto"` invokes a model only for supported image inputs. `always` emits a no-attachment marker for text-only turns without making a model call. `off` disables preflight. High detail, comparisons, dense interfaces or diagrams, low confidence, and explicit re-analysis select `visual-intake-deep`. Eligibility requires `capabilities.vision = true`, a configured credential, and a closed circuit.

Validated results and deterministic unsupported-media markers are cached for 24 hours in `~/.cache/grok/visual-intake.json` with mode 0600 and a maximum of 100 entries. Cache keys include ordered content hashes, schema and role versions, options, and binding version. Transient failures are not cached. Cache and telemetry records exclude image bytes, OCR text, filenames, paths, and credentials.

Visual intake is not an intent and does not add an execution track. To roll it back without changing text routing, set `[visual_intake] mode = "off"` in `config/config.toml`.

## Effort semantics

An explicit `[subagents.roles.<role>].reasoning_effort` overrides the parent or global default. Without a role override, the parent setting applies; the configured global default is `high`. Routing decisions retain the `(model, reasoning_effort)` pair. Spawn recipes print `model @ effort` for explicit role effort and only `model` when effort is inherited. Compatibility entries under `[subagents.models]` do not override role specifications.

Every distinguishable role must have a distinguishable effect through effort, lens, or pipeline position. A role whose selection changes nothing is an uncovered branch.

## Pipeline and gates

- Every implementation starts with profile-driven reconnaissance: three members for medium complexity, plus two additional members for high complexity. Low-complexity routes use the profile's reduced reconnaissance path.
- Medium- and high-complexity implementation includes `review-hard` or a review panel. If review is unavailable, the route may use the exact configured deterministic verifier with a warning; without either, it becomes observe-only.
- Security routing runs analysis → implementation → available review → independent security verification → ordered deterministic verifiers. Without an explicit availability signal for the independent verifier, the route is observe-only with reason `security_verifier_unavailable`.
- Direct review uses exactly one `review-hard`. Plan or ADR work first completes `plan-hard`; a writable child writes any requested artifact. `expert-rescue` diagnoses but never implements.
- Edit tools and Stop remain gated until required stages finish. During reconnaissance the conductor may use narrow `read_file` context but cannot broadly map the repository. Child sessions are gate-exempt; the conductor remains permanently zero-write.
- A background acknowledgement only binds a task ID. Completion requires retrieval of a terminal result for that ID. Typed `not_found` retrievals release the binding after two observations with cause `task_not_found` and retain a tombstone for late correlation; other failures reopen the stage directly.
- Unfinished execution debt persists within the bounded session window. Stop repeats the repair recipe up to `stop_block_limit`, resetting the count when real progress occurs.
- Deterministic verification is workspace-aware and permits only the exact configured argv.

For external or sandbox tasks, a read-only reviewer that cannot reach the artifact reports `review_inconclusive`; the stage can be discharged from typed evidence, while any deterministic verifier runs in a writable child rather than the zero-write conductor.

State-machine, tier-selection, and reason-code details are documented in [`grokbuild/README.md`](../grokbuild/README.md).

## Telemetry and stats

Runtime data is stored locally:

- Decisions and route cache: `~/.cache/grok/route.json`.
- Visual results: `~/.cache/grok/visual-intake.json`.
- Advisory model metadata: `~/.cache/grok/models.json`.
- Runtime state: `~/.local/state/grok-route/state.json`.
- Redacted append-only telemetry: `~/.local/state/grok-route/route.jsonl`, with rotations `.1` and `.2`.

Telemetry uses decision schema v4 and generation 1. Prompt text is not recorded. One deduplicated submit record is written per decision, one `pre_tool_use_outcome` per PreToolUse event, and one Stop record per Stop event. Stable reason codes distinguish inactive or exempt gates, allowed stages and reads, wrong or pending stages, reconnaissance restrictions, zero-write denials, and lifted gates.

```bash
python3 -m grokbuild.cli replay --last 20
python3 -m grokbuild.cli stats
python3 -m grokbuild.cli stats --json --since 2026-08-17T00:00:00Z
```

Intent routing is routed submits divided by all submits. Pair divergence is the share of routed submits whose effective `(model, explicit effort or default_reasoning_effort)` differs from the configured default pair; it is `null` when configuration is unavailable. Selected-pair distribution preserves raw effort and displays missing effort as `inherit`. Tier and fallback divergence measures fallback or noncanonical tiers among routed submits. Deny, gate-deny, zero-write, Stop-block, and repeated-deny friction metrics use their corresponding telemetry outcomes. Optional labels provide coverage and false-positive rate; without labels, false-positive rate is `null`.

## Verification

Run the deterministic repository checks from the checkout root:

```bash
python3 -m grokbuild.render_docs check --config config/config.toml
./tests/grok-route-test.sh
python3 -m pytest tests -q
```

The route suite exercises classification, stage gating, state, visual intake, routing tiers, discovery, workload corpus, documentation rendering, and cross-turn verification debt. The installer test uses an isolated home and validates the artifacts installed by `scripts/install.sh`.

## Rollback

Set `GROK_ROUTE_MODE=shadow` for an urgent **stage-gating rollback only**. Shadow still denies conductor writes; it does not turn this layer into a write-permissive mode and it does not restore an earlier account configuration.

For a full uninstall, first remove this project's hook registrations from the live Grok configuration, then remove only the repository-owned hook links (`~/.grok/hooks/route.py` and `~/.grok/hooks/route.json`) and repository-owned links under `~/.grok/agents`, `~/.grok/skills`, `~/.grok/rules`, and `~/.grok/routing`. Do not remove account-owned files. The installer creates account-config backups named `~/.grok/config.toml.before-combine-<UTC timestamp>.bak`; inspect the desired backup, compare it with the current file, and selectively restore only settings that belong to this project while preserving unrelated settings and later user changes. For example:

```bash
ls -t ~/.grok/config.toml.before-combine-*.bak
cp ~/.grok/config.toml.before-combine-<timestamp>.bak ~/.grok/config.toml.restore-candidate
# edit/compare the candidate, then merge only the intended project-owned removals
```

Do not rerun an older installer as a rollback: it preserves account-owned tables and can re-install the project. Existing sessions must reload hooks or restart after mode or installation changes.
