# Architecture

This document describes the post-split module boundaries and runtime artifacts in this tree. The normative routing and composition details remain in [`grokbuild/README.md`](../grokbuild/README.md).

## Layer map after the split

The primary runtime spine is:

`leaf utilities (_repo, redact, payloads, persist) → state (types/IO) → transactions (single mutation layer) → verifiers/availability → compose → pipeline (markers, scoring, role selection, Pipeline) → gate/router → transcript/hook/settlement → cli/conductor/stats/render_docs/corpus_sync/discovery`

This is a responsibility map rather than a claim that every import follows one straight line. `decision.py` supplies cross-cutting immutable wire types, while `classify.py`, `features.py`, `policy.py`, and `roles.py` supply classification and registry data to the runtime spine. `cache.py`, endpoint/failure/remediation sidecars, visual intake, and shell safety are bounded support branches.

`state.py` owns state types, serialization, reads, and compatibility migrations. `transactions.py` is the single mutation layer for runtime state: callers do not reproduce stage-result mutation blocks. `verifiers.py`, `availability.py`, and `compose.py` isolate command resolution, endpoint/role availability, and execution-stage construction respectively; `pipeline.py` retains core prompt markers, complexity/risk and candidate scoring, role selection, `decision_outcome`, and `Pipeline` orchestration.

## Module map

All 44 `grokbuild/*.py` modules present in this tree are covered below. “Consumers” names the principal in-tree runtime consumer; modules with no active consumer are identified explicitly.

| Module | Responsibility, key exports, and consumers |
| --- | --- |
| `__init__.py` | Package marker and metadata for cooperative routing and stage gating. Imported whenever the package or package submodules are loaded. |
| `_repo.py` | Resolves checkout and configuration anchors through `repo_root()` and `config_path()`. Consumed by `verifiers.py`. |
| `artifact_judge.py` | Pure, identity-blind comparison of candidate artifacts with deterministic hard-requirement precedence. Exports `Requirement`, `TaskContract`, `Candidate`, `Bundle`, `JudgeOpinion`, `Verdict`, `compare()`, `fold_opinions()`, `hard_precedence()`, and `sanitize()`; consumed only by dormant `selector.py`. |
| `availability.py` | Evaluates credentials, cached quota signals, endpoint rotation, provider holds, and role availability without composing stages. Its key internal interfaces are `_model_endpoint_availability()`, `_role_availability()`, `_role_available()`, and quota helpers; consumed by `pipeline.py`, `settlement.py`, and `hook.py`. |
| `cache.py` | Maintains the bounded transactional route-decision cache keyed by session, turn, and prompt. Exports `default_cache_path()`, `cache_key()`, `cache_get()`, `cache_put()`, `CACHE_VERSION`, and `DEFAULT_TTL`; consumed by `hook.py`. |
| `classify.py` | Loads intent dictionaries, extracts trusted user text, normalizes prompts, matches needles, and recognizes synthetic Stop feedback. Key exports include `load_intents()`, `extract_user_text()`, `normalize()`, `match_needles()`, and `model_allowed()`; consumed by `features`, `pipeline`, `gate`, `hook`, `transcript`, `stats`, `cli`, and `corpus_sync`. |
| `cli.py` | Implements the `grok-route` operator CLI for explain, state, replay, roles, stats, profiles, visual intake, discovery, contracts, and version reporting. `build_parser()` and `main()` dispatch the `cmd_*` handlers; it is the command-line entry point. |
| `compose.py` | Builds and validates active execution stages, barriers, review panels, consilium, and verifier stages. Key exports are `compose_execution()`, `compose_consilium_barrier()`, `load_barrier_lenses()`, `validate_execution()`, and `has_required_execution()`; consumed by `pipeline.py`, transaction repair escalation, and `eval/composition.py`. Its tail also contains explicitly dormant control-plane composers described below. |
| `conductor.py` | Resolves the pre-session conductor from the configured fallback chain without exposing credentials. Exports `resolve_conductor()` and conductor constants; consumed by `cli.py` and `render_docs.py`. |
| `corpus_sync.py` | Harvests redacted route outcomes and local git evidence into bounded, manually adjudicated workload staging. Its `import_history()`, `import_git()`, `merge()`, `triage()`, `status()`, and `main()` functions serve the standalone corpus-sync workflow. |
| `criterion_judge.py` | Produces a pure per-criterion verdict from structured evidence after hard deterministic checks. Exports `Verdict` and `judge_criterion()`; it is DECLARED GROUNDWORK with no active route consumer. |
| `datasets.py` | Defines bounded, redacted, versioned control-plane records for escalation, judge calibration, verification plans, and attribution. Exports the four record dataclasses plus `record_*()`, `read_records()`, `dataset_path()`, and `escalation_summary()`; it is collection groundwork and has no active route consumer. |
| `decision.py` | Defines the versioned routing and execution wire types: `RouteDecision`, `ExecutionStage`, `ExecutionMember`, `Candidate`, and `ConductorHandoff`. It exports serialization helpers `decision_to_dict()`, `decision_from_dict()`, `decision_hook_record()`, and `hash_id()` and is consumed throughout pipeline, composition, state, transactions, router, hook, CLI, stats, conductor, and evaluation code. |
| `discovery.py` | Builds advisory provider discovery targets, fetches model/quota snapshots, and reads or compares the cache without changing routing pins. Exports `DiscoveryTarget`, `sync()`, `load_targets()`, `load_cache()`, `stale_targets()`, `diff()`, and `quota_report()`; consumed by `cli.py` and bounded stale-quota refresh in `availability.py`. |
| `endpoint_resolution.py` | Stores versioned out-of-band records explaining which endpoint was selected and which were skipped. Exports `EndpointResolutionRecord`, `make_endpoint_resolution()`, `persist_endpoint_resolution()`, `latest_resolution_for_decision()`, and `endpoint_resolution_path()`; produced by `pipeline.py` and read by `settlement.py`/`hook.py`. |
| `evidence.py` | Represents runtime failure signals and classifies operational failure causes such as auth, environment, and model failures. Exports `FailureSignal`, `EvidenceRecord`, `FailureCause`, `classify_failure()`, `make_evidence()`, `persist_evidence()`, and `evidence_path()`; consumed by availability, pipeline, transactions, and settlement. |
| `features.py` | Converts a prompt into `FeatureSet`, including normalized text, override/modifier tokens, and intent hits. Exports `FeatureSet`, `extract_features()`, `detect_override()`, and `detect_modifier()`; consumed by `pipeline.py` and `policy.py`. |
| `frontier.py` | Purely scores observable ambiguity/coupling signals to decide whether expensive meta-planning would be justified. Exports `FrontierSignals`, `FrontierDecision`, `frontier_score()`, and `decide()`; it is DECLARED GROUNDWORK with no active route consumer. |
| `gate.py` | Evaluates PreToolUse/Stop stage gates, debt, exact verifiers, and next-station recipes. `station_recipe()` is the public recipe formatter and the gate evaluators are used by `hook.py`; it consumes state, transactions, policy, roles, transcript, verifiers, and safe-alternative context. |
| `hook.py` | Implements the Grok hook integration and terminal allow/deny responses. Key entry points are `resolve_route()`, `handle_prompt()`, `handle_pre_tool()`, `handle_stop()`, and `main()`; it configures and delegates PostToolUse settlement to `settlement.py`. |
| `payloads.py` | Parses spawn, background acknowledgement, retrieval, and terminal task payloads independently of the live hook. Exports `tool_input()`, `task_ids()`, `background_ack_id()`, result/status classifiers, transcript synthesis, and `dump_payload_debug()`; consumed by hook, settlement, transcript, and shell guard. |
| `persist.py` | Supplies leaf persistence primitives: XDG paths, JSONL iteration, redacted atomic writes, locking, migration reads, and rotation. Key exports include `state_dir()`, `sidecar_path()`, `sidecar_generations()`, `iter_jsonl()`, `atomic_update_json()`, `atomic_write_json()`, `append_jsonl()`, and `parse_iso_utc()`; consumed by all state, cache, telemetry, dataset, evidence, and sidecar modules. |
| `pipeline.py` | Owns active routing core logic: prompt markers, complexity/risk computation, candidate scoring, implementation-role selection, the telemetry view `decision_outcome()`, and `Pipeline`. It re-exports composition and verifier compatibility names, orchestrates classification/policy/roles/availability/compose/state, and is consumed by `router.py` and `hook.py`. |
| `policy.py` | Loads profiles and implements scored intent policy, mode resolution, thresholds, and explicit overrides. Exports `Profile`, `ScoredIntent`, `load_profiles()`, `score_intents()`, `pick_winner()`, `gated_intents()`, `resolve_mode_from_env()`, and `is_truthy_env()`; consumed by pipeline, compose, gate, router, hook, state pruning, settlement, remediation, CLI, and evaluation. |
| `redact.py` | Provides conservative recursive and text redaction at persistence boundaries. Exports `redact()` and `redact_text()`; consumed by persist, state, cache, hook, corpus sync, and artifact sanitization. |
| `remediation.py` | Owns the bounded session-scoped remediation-debt sidecar and Stop context. Exports `RemediationDebt`, `record_remediation_attempt()`, `record_remediation_stop()`, `open_remediation_debt()`, `open_remediation_debts()`, `compact_remediation_debt()`, and `remediation_stop_context()`; consumed by transactions, hook, and CLI. |
| `render_docs.py` | Renders and validates checked-in contract projections from repository configuration. `render_all()`, `validate()`, projection renderers, and `main()` are used by release/documentation checks. |
| `roles.py` | Loads and validates stable role names, model bindings, provider metadata, capabilities, families, and credential presence. Exports `Role`, `RoleRegistry`, capability/provider dataclasses, `load_registry()`, `load_provider_catalog()`, `family_of()`, `provider_from()`, and `role_credential_present()`; consumed by nearly every routing, composition, availability, gate, settlement, discovery, visual, stats, and documentation layer. |
| `router.py` | Provides the thin routing facade `route_prompt()`, which constructs `Pipeline` and returns a `RouteDecision`. Consumed by CLI and corpus sync. |
| `safe_alternatives.py` | Holds reviewed, bounded denial hints and terminal-operation classification. Exports `SAFE_ALTERNATIVES`, `classify_operation()`, and `safe_alternative()`; consumed by `hook.py`. |
| `selector.py` | Selects among candidate **artifacts**, clusters equivalent outputs, and measures oracle regret; it does not select routing roles or agents. Exports `ArtifactCluster`, `Selection`, `cluster_artifacts()`, `select()`, `select_v2()`, `oracle_regret()`, and `selector_capture()`; this v2 benchmark arm is DECLARED GROUNDWORK with no active route consumer. |
| `settlement.py` | Owns PostToolUse and PostToolUseFailure correlation: child/retrieval/verifier outcomes, debt binding, task evidence, endpoint failure feedback, and transaction calls. Exports `configure()`, `handle_post_tool()`, and `handle_post_tool_failure()`; configured and consumed by `hook.py`. |
| `shell_guard.py` | Classifies read-only shell commands and reconnaissance-safe command chains. Exports `is_readonly_shell()` and `is_recon_shell()`; consumed by hook and settlement. |
| `state.py` | Defines runtime state, execution tracks, role/provider status, circuit breakers, history, debt views, compatibility loading, and state-file IO. Exports `RuntimeState`, `ExecutionTrack`, `RoleStatus`, `ProviderAvailability`, `load_state()`, `save_state()`, `pending_child_bindings()`, `current_turn_tx()`, and default path helpers; read throughout the runtime, while mutation operations live in `transactions.py`. |
| `stats.py` | Reads route telemetry and computes routing, gate, fallback, provider, and visual metrics. Exports `log_paths()`, `read_records()`, `compute_stats()`, and `human_summary()`; consumed by `cli.py`. |
| `task_evidence.py` | Owns typed task contracts/results and the bounded task-outcome evidence JSONL sidecar. Exports `TaskSpec`, `TaskResult`, `EvidenceOutcome`, `attach_task_spec()`, `find_task_spec()`, `record_task_evidence()`, `parse_task_result()`, `observe()`, `verify_task_result()`, and `decide_completion()`; this is the active R2 support path used by settlement under the hook. |
| `task_verify.py` | Purely verifies a typed `TaskResult` against a `TaskSpec` and caller-supplied `Observation`; it does not perform IO or run commands. Exports `Observation`, `Verification`, `REASON_CODES`, and `verify()`; consumed by `task_evidence.py` in the active hook settlement path. |
| `tiers.py` | Derives provider subscription coverage and resolved role sets from checked-in role/provider data and credential presence. Key functions are `role_provider_map()`, `roles_for_providers()`, `provider_credentials()`, `resolved_roles()`, and `achieved_level()`; used by setup/contract tooling and tests rather than active routing. |
| `transactions.py` | Implements every transactional mutation of runtime state, including turn allocation, execution reconciliation, task binding, stage/retrieval settlement, Stop counters, and verifier fanout. Its `*_tx` API is consumed by pipeline, gate, hook, settlement, and CLI; `_apply_stage_result()` is the one internal stage-result mutation core. |
| `transcript.py` | Reads session transcripts, finds the last real user turn, canonicalizes station roles, and summarizes turn-stage status. Key exports include `last_user_prompt()`, `last_user_prompt_raw()`, `last_user_turn_number()`, and `turn_station_status()`; consumed by gate, hook, and settlement. |
| `verifier_planner.py` | Purely describes what additional evidence would discriminate unresolved artifacts; it never runs the experiment. Exports `ChangeMap`, `NeedEvidence`, `AttributionOutcome`, `analyze()`, `attribute_evidence()`, and `evidence_stage_spec()`; it is DECLARED GROUNDWORK used only by the dormant tail of `compose.py`. |
| `verifiers.py` | Parses verifier argv, loads workspace verifier maps, applies config overrides, resolves exact commands, and checks command identity. Exports `parse_verifier_argv()`, `load_verifier_map()`, `apply_config_verifiers()`, `resolve_verifier()`, and `is_verifier_command()`; consumed by pipeline compatibility exports, compose/gate/hook/settlement, and transactions. |
| `visual_cache.py` | Stores bounded, expiring, validated visual-intake results. Exports `default_visual_cache_path()`, `visual_cache_get()`, `visual_cache_put()`, `visual_cache_stats()`, and `visual_cache_clear()`; consumed by visual intake and CLI. |
| `visual_intake.py` | Defines schema-v1 visual request/result/error types, validates and compacts visual context, selects normal/deep intake, and coordinates runtime-owned invocation and caching. Key exports include `VisualIntakeResultV1`, `VisualIntakeRequestV1`, `validate_visual_intake_result()`, `resolve_visual_role()`, `run_visual_intake()`, and `request_visual_analysis()`; consumed by CLI and the external TUI intake lifecycle. |

## Declared control-plane groundwork

`frontier.py`, `selector.py`, `artifact_judge.py`, `datasets.py`, `criterion_judge.py`, `verifier_planner.py`, and the tail block in `compose.py` are **DECLARED GROUNDWORK (AWAITING DATASET; not wired to active routes)**. The dormant compose block contains `compose_frontier_stage()`, `compose_judge_panel()`, `compose_evidence_stage()`, and `compose_adjudication_stage()`; compatibility re-exports do not make those stages active.

The naming traps are intentional and important:

- `selector.py` performs **artifact selection** for the v2 benchmark arm. Active role selection remains `pipeline.select_implement_role()` and pipeline candidate scoring; selector never chooses an agent or model.
- `evidence.py` records and classifies **runtime failure signals** used by availability, settlement, and circuit handling.
- `task_evidence.py` stores **task-outcome evidence JSONL** and typed task contracts/results. Together with pure `task_verify.py`, it is the one active support path in this group, reached through settlement configured by `hook.py`.
- `task_verify.py` contains **verification planning/operations for typed tasks** in the narrow sense of evaluating caller-provided observations; it performs no tools, filesystem reads, routing, or spawning. Control-plane requests for new discriminating evidence belong instead to dormant `verifier_planner.py`.

## Decision serialization views

There is one typed source, `decision.RouteDecision`, and one shared base serializer, private `decision._route_decision_base()`. Two views project that base without independently recomputing fields:

- `pipeline.decision_outcome()` is the compact, redaction-safe telemetry/state-history view. It keeps the canonical `model` and `reasoning_effort` names and omits hook-only compatibility details.
- `decision.decision_hook_record()` is the hook compatibility view. It maps the same base model pair to `required_model` and `required_effort` and retains legacy helper keys such as `intent`, `current_model`, `has_strong`, `how`, and `session_id`.

Changes to common decision fields belong in the shared base first so the two serializations cannot silently drift.

### Lazy imports that are load-bearing

`RuntimeState.prune()` lazily imports `load_profiles`. Transaction repair escalation lazily imports `compose_consilium_barrier` from `compose.py` and `load_registry` from `roles.py`; the deferred `transactions` imports in state-facing compatibility functions occur only after state types are defined. These imports must stay lazy because state and transactions sit below policy/composition, while consilium composition is an exceptional higher-layer path; making them eager would create circular initialization.

## State on disk

Paths are rooted at `XDG_STATE_HOME/grok-route/`, defaulting to `~/.local/state/grok-route/`, and at `XDG_CACHE_HOME/grok/`, defaulting to `~/.cache/grok/`. Corpus-sync artifacts are hardcoded to `~/.local/state/grok-route/` because `corpus_sync.py` ignores `XDG_STATE_HOME`; this inconsistency is documented fact.

| Path | Contents and bounds |
| --- | --- |
| `XDG_STATE_HOME/grok-route/state.json` | Transactional runtime state, role/provider status, bounded decision history, execution tracks, and turn bookkeeping. State is stale after 24 hours; history is bounded at 400 records during rotation and retained at 200. |
| `XDG_STATE_HOME/grok-route/state.json.lock` | Lock used for atomic state updates. |
| `XDG_STATE_HOME/grok-route/decisions.jsonl` | Redacted decision-history migration and append log; `.decisions-migrated` records the one-time migration marker. |
| `XDG_STATE_HOME/grok-route/.decisions-migrated` | Decision-history migration marker. |
| `XDG_STATE_HOME/grok-route/route.jsonl` | Redacted append-only hook telemetry. It rotates at 2 MiB and retains `route.jsonl`, `.1`, and `.2`. |
| `XDG_STATE_HOME/grok-route/route.jsonl.1`, `route.jsonl.2` | The two chronological telemetry rotations. |
| `XDG_STATE_HOME/grok-route/route.jsonl.lock` | Telemetry append/rotation lock. |
| `XDG_STATE_HOME/grok-route/evidence-v1.jsonl` | Versioned runtime failure evidence emitted by `evidence.py`. |
| `XDG_STATE_HOME/grok-route/endpoint-resolution-v1.jsonl` | Endpoint selection and skipped-endpoint evidence. |
| `XDG_STATE_HOME/grok-route/r2-evidence-v1.jsonl` | Typed task specifications, results, observations, and verification outcomes. |
| `XDG_STATE_HOME/grok-route/remediation-debt-v1.jsonl` | Bounded session remediation attempts and Stop debt context. |
| `XDG_STATE_HOME/grok-route/corpus-sync.json` | Corpus-sync watermark and bounded import bookkeeping when corpus synchronization is used. |
| `XDG_STATE_HOME/grok-route/workloads-staging.json` | Bounded workload candidates awaiting triage or merge; the staging cap is 500 entries, with the importer clamping operational caps to 200 where required. |
| `XDG_STATE_HOME/grok-route/corpus-sync.lock` | Corpus synchronization lock. |
| `XDG_STATE_HOME/grok-route/payload-debug.enabled` | Explicit opt-in marker for task-payload diagnostics. |
| `XDG_STATE_HOME/grok-route/payload-debug.jsonl` | Raw task-payload captures only while payload debugging is explicitly enabled. |
| `XDG_CACHE_HOME/grok/route.json` | Route-decision cache with a 900-second TTL and at most 50 entries. |
| `XDG_CACHE_HOME/grok/visual-intake.json` | Validated visual result cache with a 24-hour TTL and at most 100 entries. |
| `XDG_CACHE_HOME/grok/models.json` | Advisory upstream model and quota discovery cache. |

State and cache files are written with mode `0600`; state/cache directories are created with mode `0700`. Generic state and telemetry lock sidecars are created with plain `open()` and inherit umask-dependent modes; only the corpus lock is explicitly `0600`. Payload-debug is opt-in because it can contain raw task-tool structures; normal telemetry remains redacted.

## Stage and gate lifecycle

The active routing trace is ordered as:

`extract → features/modifier/override → score → role → availability → compose → policy gate → record`

A stage has one of these execution kinds:

- `spawn`: one named child role, consuming its linear stage slot.
- `parallel_spawn`: a keyed barrier whose members consume distinct slots, such as `recon/0`, `recon/1`, `recon/2`, `review-panel/0`, `review-panel/1`, or `consilium/0`, `consilium/1`, `consilium/2`.
- `verify`: a non-spawnable deterministic verifier tracked as `verify/0` through `verify/N` and admitted only by exact configured argv.
- `sentinel`: an explicit unavailable or observe-only condition, including `consilium-unavailable`; it is a state marker, not a child spawn.

The hook loop is submit → pre-tool spawn gating → post-tool settlement/correlation → exact-argv verifier → gate lift. `UserPromptSubmit` classifies and persists. `PreToolUse` allows only the next linear stage or an outstanding barrier member, while denying edits during pending work and applying the reconnaissance diet. `settlement.py` handles PostToolUse/PostToolUseFailure and invokes the transaction layer to bind and complete the matching child or verifier result. A quota/429 failure opens the provider circuit on every path it can arrive on: a synchronous spawn result, a `PostToolUseFailure`, a failed section of a `get_command_or_subagent_output` retrieval (the child's role from its stage binding or the section's `Command: [subagent:<type>]`, the provider from the child's own session model id), and a `BLOCKED` child found by the Stop-time sweep. `PreToolUse` binds explicit-endpoint roles to a healthy endpoint (`updatedInput.model`) for ungated spawns as well as gated ones, so a held provider is not hit again by an ad-hoc spawn after the pipeline finished; the routing decision itself is untouched. Stop blocks unfinished debt and retries the bounded repair recipe until the debt window or Stop limit is reached. A successful verifier lifts the remaining gate; a failed spawn, retrieval, or verifier reopens its stage.

Per-intent active compositions are defined by `grokbuild/README.md` and built in `compose.py` under `Pipeline` orchestration:

| Intent | Composition |
| --- | --- |
| Low-complexity implement | One `explore` spawn → selected implementation → ordered `verify/0..N`. |
| Medium-complexity implement | Profile-driven `recon` barrier (default three members) → implementation → `review-hard` → ordered verifiers. |
| High-complexity implement | Profile-driven recon barrier (default five members); optional `plan-hard` → implementation → review barrier or degraded review → verifiers. |
| Security | `security` analysis → implementation → optional independent review → `security-verify` → ordered verifiers; unavailable required security verification makes the route observe-only. |
| Review | Exactly one read-only `review-hard`; no implementation or verifier stage. |
| Plan / ADR | Required read-only `plan-hard`; a writable child creates a requested artifact only as separately composed by the caller. |
| Explore | Required read-only `explore`; edits and Stop remain gated until it completes. |

Medium and high implementation requires review or, when review is unavailable, an exact configured verifier with a warning. With neither, the route is observe-only. Repeated failed repair stages can create the three-member cross-provider consilium barrier; if it cannot be composed, `consilium-unavailable` preserves the failure rather than pretending the stage completed. The conductor remains zero-write regardless of mode or stage completion.

Verification assumes the artifact is reachable. For external/sandbox tasks (docker-exec, remote wrappers), a read-only reviewer may be unable to inspect the artifact directly; in that case the reviewer's BLOCKED “cannot verify” report is treated as `review_inconclusive`, not a failed review: the stage is discharged and the harness accepts a static conclusion based on the typed evidence already gathered. A deterministic task verifier, when the task defines one, is executed by a writable child—never by the zero-write conductor.

This document records the implementation present in this tree. Dormant modules are explicitly labeled; their presence is not an active routing or enforcement guarantee.
