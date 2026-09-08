# Failover risk-aware allocation design note

## The motivating incident, per verified records

The following timeline is external ratified evidence from a dedicated read-only extraction of `decisions.jsonl`, `route.jsonl`, `state.json`, and session records. On 2026-08-27 at 19:31 UTC, data-warehouse operations/cleanup session `01a044b5` was created with current model `glm-5.3-flash`. During 2026-08-27 and 2026-08-28, backfill attempts recorded three identical `243 NOT_ENOUGH_SPACE` failures from temporary-disk spill in an `implement-ops` context. On 2026-08-28, a `gpt-5.6-luna` child stopped after about ten turns with a transient upstream `401`; the router switched to cross-provider overflow (`deepseek-v4-pro`) as a workaround. A late continuation records explicit upstream OIDC `401 Unauthorized` after refresh and is titled “overflow after 401 luna”.

At 2026-08-30T08:14:33Z, decision `a9ee75e8119b291fbfab` recorded the overflow selection for session `01a044b5`: intent `implement`, complexity `low`, risk `medium`, and default tier `standard`. `implement-standard`, `implement-hard`, and `implement-cheap` were skipped with generic `unavailable/pressured/failed` labels; `implement-overflow` was chosen with warnings that the route degraded from `implement-standard` to `implement-overflow`. The stage trace was all successful; `would_deny_edits=true`, `would_block_stop=true`, and `enforce=false`. At present, `implement-hard` is unavailable after three consecutive “spawn result failure” failures with an expired circuit; the `commandcode` hold is expired and everything else is available.

The three competing narratives adjudicate as follows:

| Narrative | Adjudication |
| --- | --- |
| N1: quota burn caused the failover | **REFUTED.** The verified records do not show exhausted quota windows. |
| N2: disk-full failures opened the model circuits | **NOT DEMONSTRATED.** The disk failures are real, but the causal link to circuit openings is unproven; alignment did not converge. |
| N3: upstream `401` led to overflow | **PARTIAL.** The `401` failures and “overflow after 401 luna” marker are evidenced, and overflow followed skipped primary tiers, but the complete per-role causal chain is unrecorded because skip labels are generic. |

The following remain explicitly unverifiable from the layer's own records: which exact cause produced each skipped role; whether any quota pressure caused a skip; whether the three disk errors opened or contributed to a model circuit; and the precise per-role transition from the upstream `401` to the final overflow decision. The 891-disk-errors figure is only a session-identification/distribution statistic from incident-review memory, not a field of the overflow decision and not proof that disk errors opened circuits.

The central conclusion is not that this incident proves one failover story. It is that the layer's records could not attribute failure causes: generic labels and state snapshots rather than event streams forced manual digging, allowing three narratives to compete while one was refuted, one was not demonstrated, and one remained partial. Failure-cause attribution is therefore the direct justification for Slice-1b.

## Problem statement and structural gaps

### Model identity is bound to one provider

The provider catalog is a strict `model_id -> ProviderModelMeta` single-valued mapping, loaded by `roles.py`. A model identity therefore has exactly one upstream provider binding. The same upstream model cannot be served from two subscriptions, so a burned or unavailable model has no model-centric cross-provider failover path.

### Overflow has no risk restriction

`implement-overflow` is selectable for any computed risk, including high-risk or production work. The only existing low-risk restriction is attached to `implement-cheap-fallback`: it requires low risk, a testable task, and a verifier. Consequently, when stronger tiers are unavailable, an overflow role can remain a weak but nominally selectable survivor for work whose risk requires a stronger class.

### Exhaustion silently degrades

When capable implementation tiers are exhausted or unavailable, the pipeline can end with “no available implement tier ...; observe-only (no gate).” There is no explicit `BLOCKED`/escalate outcome telling the operator that no model of the required class is available and quota or human intervention is needed. This is the dangerous allocation behavior addressed by Slice-1.

## Design goals and boundaries

The design separates two dimensions:

1. **Risk-floor and escalation:** never use a role below the risk floor merely because it is the last available role.
2. **Model endpoint failover:** retain tier-based model selection while selecting among ordered provider subscriptions for that logical model.

Failover is cooperative. It does not promise enforcement against a provider, child process, or operator override. Model selection remains with the tier ladder, which expresses capability and risk; endpoint selection is an inner availability loop.

## Slice-1: risk floor and explicit escalation

Slice-1 is the smaller first slice and stops the worst mode: silent production work on the weakest surviving role.

### Proposed contract

Add an optional per-role `risk_ceiling` field with this normative matrix for implementation roles:

| Role | `risk_ceiling` policy |
| --- | --- |
| `implement-overflow` | **`medium`**, mandatory for Slice-1; this is the restrictive ceiling that prevents weak overflow on high-risk work. |
| `implement-cheap-fallback` | Unchanged existing stricter gate: low risk, explicit testability, and a known verifier. This is a separate gate, not a replacement ceiling. |
| `implement-ops` | Field absent; unrestricted, effectively `high`. |
| `implement-hard` | Field absent; unrestricted, effectively `high`. |
| `implement-standard` | Field absent; unrestricted, effectively `high`. |
| `implement-cheap` | Field absent; unrestricted, effectively `high`. |

Read-only roles are out of scope. An absent `risk_ceiling` is a compatibility value meaning unrestricted, effectively `high`; it does not add a new restriction to the primary ladder, whose tier order already prefers stronger classes. Existing risk computation remains authoritative: high risk is produced by security intent, risk terms, destructive work, or high-complexity implementation.

The selector rejects a role whose ceiling is below the computed risk before checking availability, with the distinct reason `risk ceiling exceeded`. This ordering prevents quota or circuit state from obscuring a policy rejection. With the normative `medium` ceiling, high-risk work never selects `implement-overflow`. If no role remains after the ceiling and availability checks, the `BLOCKED`/escalate outcome fires.

If no permitted role is available for high-risk work, the route outcome becomes `BLOCKED` plus an escalation instruction: “no capable+available model of class X; quota/human needed.” The new blocked/escalation field or reason code must independently drive both `would_deny_edits=true` and `would_block_stop=true`, regardless of `role_spawnable`. In the current path (`grokbuild/pipeline.py` approximately lines 1618-1627), one `initial_gate` value controls both booleans; adding only a route reason would silently lose the operator-visible Stop block. The field or reason must therefore be serialized, consumed by the hook, and covered by tests asserting both booleans for high-risk exhaustion. This is an operator-visible outcome, not observe-only degradation.

Slice-1 does not alter model/provider identity, endpoint resolution, or the data-warehouse session's correct `implement-ops` routing. The motivating incident was risk=`medium`, so the normative `implement-overflow`=`medium` ceiling would not have caught it. Slice-1 bounds the silent-weakest-model class for **high-risk** work; the medium-risk production policy question remains open below.

### Slice-1 validation

Offline validation is deterministic and profile/registry-driven. Composition assertions should cover ceiling filtering before availability, the exact `risk ceiling exceeded` reason, preservation of permitted standard/ops/hard routes, and `BLOCKED` plus escalation when no high-risk-capable role remains. Live validation follows the manual protocol pattern in `docs/eval/live-protocol.md`: use a locked profile and registry, record the resolved decision and reason, and manually verify the operator-visible outcome. No provider call is required for the offline cases.

## Slice-1b: failure-cause attribution

Slice-1b replaces the former environmental-only framing. Every failure evidence record carries a cause class: `auth/credential`, `environment/infrastructure`, `model`, or `unknown`. `unknown` is the conservative default and currently counts on the model path, while the schema remains extensible when stronger evidence becomes available. This incident demonstrates the value directly: authentication failures, temporary-disk failures, and the rejected quota narrative could not be distinguished from the layer's generic records; cause attribution makes that distinction immediate.

Environment-class failures do not open model circuits and do not count toward model degradation. Persistent failures of any class still escalate through Slice-1's outcome, with the class in the operator message: `model-class: quota/human needed` for model-side exhaustion or `infrastructure: human needed` for infrastructure failure. Auth/credential failures should retain their class rather than being silently treated as quota or model failure.

Validation adds deterministic cause-class fixtures and event-to-decision assertions: the three causes in this incident must remain distinguishable, environment failures must leave model degradation unchanged, and unknown failures must conservatively follow the current model path. Live validation follows the manual protocol with captured evidence and explicit missing-data handling.

## Slice-2: model-centric endpoints and quota-aware failover

Slice-2 adds ordered provider endpoints to a logical model and performs quota-aware endpoint selection. It follows Slice-1 because endpoint resilience must not bypass the risk floor. This incident did not require same-model endpoint failover: overflow/deepseek was healthy. Slice-2 is future compound resilience for a primary auth/endpoint failure combined with reserve quota exhaustion, and it supports the operator's cross-subscription deepseek goal; it is not incident remediation.

### Model-centric endpoint schema options

The recommended representation is an optional model-level `endpoints` array in `providers.json`. A logical model then owns an ordered list of provider endpoints, with the primary endpoint first according to the existing `subscription_class` and reserve endpoints following it. Endpoint records would carry provider identity, subscription class, credential binding metadata, and endpoint-specific availability data as appropriate. This is architecturally clean: roles continue to request a logical capability/model while the allocator resolves a subscription endpoint.

A second option is a separate mirror catalog key for each provider's copy of the model. This requires fewer parser changes because each key remains a conventional one-to-one catalog entry. It also risks exposing provider identity in role-facing configuration, repeating the rejected mirror-role approach's coupling in a catalog-shaped form. It duplicates model metadata and makes ordering and logical-model equivalence harder to state and validate.

The endpoint array is recommended despite its larger implementation cost. It requires a strict-schema extension in `render_docs`, endpoint-aware catalog loading in `roles.py`, discovery probing per endpoint, and an explicit semantics decision in `tiers.py` and `test_tier_matrix.py`. Those costs buy a stable logical model contract and keep subscription topology out of role names.

Compatibility is deliberately conservative: an absent or optional `endpoints` field means the existing single provider binding and is interpreted as a single-endpoint list. Existing behavior is unchanged. Golden bytes remain unaffected because `providers.json` is not part of state-v8 golden serialization; the strict `render_docs` schema extension is the only existing validation surface that must change for the catalog form.

### Endpoint selection inner loop

The tier ladder first selects a logical model based on capability and risk. For that model, the allocator evaluates endpoints in declared order and chooses the first endpoint satisfying all of these conditions:

- the endpoint's credential is present;
- the provider is not held;
- the endpoint's quota is currently acceptable;
- `require_availability_signal` is satisfied: when the provider/model requires a positive availability signal, that signal is present and non-stale. A missing or stale signal is typed unavailability, not implicit availability.

A failed endpoint advances the inner loop; it must not silently select a lower-capability model or role. If all endpoints fail, the allocator returns a typed unavailability reason to the outer tier logic, which may try the next permitted model in that tier. Risk ceilings still apply before availability, and exhausted tiers eventually produce the explicit Slice-1 escalation rather than observe-only degradation.

The discovery quota cache is currently provider-keyed. Per-endpoint probing requires endpoint-keyed cache entries. Providers with shared quotas must not claim model-level precision: a quota result for a shared provider can establish provider-level pressure, but cannot honestly assert independent remaining quota for each model endpoint. Circuit and quota pressure are role-scoped today, so the implementation must map endpoint state onto role state explicitly: retain role-scoped circuit decisions for the capability contract while carrying provider/endpoint availability as a separate inner-loop input, or define a documented projection from endpoint failure to the affected role. It must not infer endpoint availability from a role name.

### Availability state and state-v8 compatibility

Current provider holds are provider-name-keyed in `ProviderAvailability`, and the state-v8 golden fixture contains provider-availability entries. Three storage choices are possible:

1. **Out-of-band endpoint store:** retain state-v8 bytes exactly and keep endpoint holds, quota observations, and probe timestamps in a separate versioned store keyed by endpoint. This is the safest migration and is recommended because it avoids changing persisted state bytes while the endpoint contract matures.
2. **Additive optional state field:** add an endpoint-availability field that is absent by default. This can preserve old records in principle, but every serializer, loader, migration, and golden assertion must prove absence-by-default behavior; accidental empty-field emission would change bytes.
3. **State version bump:** make endpoint availability a first-class state-v9 field. This is the clearest long-term schema, but requires intentional migration and new golden fixtures and cannot claim v8 bit stability.

The recommendation is the out-of-band store, with provider-level state remaining the conservative fallback when endpoint data is missing. Discovery writes endpoint-keyed cache entries; resolution records the endpoint chosen and the reasons endpoints were skipped without rewriting state-v8 golden bytes.

## How the verified incident maps to each slice

Slice-1a would not have caught the motivating overflow: its recorded risk was `medium`, and the normative `implement-overflow` ceiling is `medium`. It does protect high-risk work from silently falling to overflow and then escalates when no permitted role remains. Slice-1b directly addresses the incident's unresolved diagnosis by recording auth, environment, model, or unknown cause classes and preventing environment failures from opening model circuits or counting toward model degradation.

Slice-2 was not required for this incident because overflow/deepseek was healthy. Its future value is compound resilience when a primary auth or endpoint failure coincides with reserve quota exhaustion, while preserving the operator's cross-subscription deepseek failover goal. This is future resilience, not remediation of the verified incident.

## Open policy questions for the operator

1. Are medium-risk production tasks silently taking `implement-overflow` acceptable, or should they also escalate?
2. Should auth-class failures on primary endpoints produce operator-visible credential-expiry alerts?
3. Does production work on a data platform need a higher risk classification than `medium`, or is this a taxonomy question requiring revised signals?
4. Should generic decision-record skip labels (`unavailable/pressured/failed`) be replaced by cause classes? This is the Slice-1b design question.

## Migration, tests, and compatibility

Migration proceeds in two slices. Slice-1 adds optional role ceilings and a typed blocked/escalate outcome while preserving existing ceilings for roles that do not declare one. Slice-2 extends the provider schema, strict rendering validation, endpoint-aware catalog loading, endpoint-keyed discovery cache, and endpoint resolution. Existing one-to-one catalogs are projected to one endpoint with zero behavior change.

The tier semantics decision is explicit: a multi-endpoint logical model carries subscription class on its endpoints; tier derivation and role-provider derivation use the primary endpoint projection, while failover occurs at endpoint selection. This avoids changing tier identity when a reserve subscription is used. If later evidence requires reserve endpoints to alter capability or tier, that is a separate semantics revision with new matrix tests.

Expected test impact includes:

- `test_degraded_fallback` legitimately changes to assert risk-floor escalation where the old result was observe-only;
- `test_overflow_after_primary_tiers` gains a high-risk ceiling case;
- `test_security_no_implement_executor` may change from observe-only to `BLOCKED` plus escalation;
- `test_conductor_resolution` remains unchanged;
- provider-schema and `render_docs` tests add endpoint-array and legacy single-endpoint cases;
- discovery tests cover endpoint-keyed quota probes, held endpoints, shared-quota imprecision, and ordered primary/reserve selection; ordered-failover tests must include a primary endpoint whose `require_availability_signal` is present-but-missing and present-but-stale, asserting typed unavailability for that endpoint and advancement to the next eligible endpoint in declared order;
- tier-matrix tests cover primary-endpoint projection and unchanged legacy behavior;
- state-golden tests continue to assert bit-stable v8 serialization when using the recommended out-of-band store.

Offline checks must be deterministic, using profile/registry fixtures and composition assertions. Live checks use the manual protocol in `docs/eval/live-protocol.md`, with endpoint credentials and quota states supplied by an operator, and report unavailable or missing telemetry rather than infer failover success.

## Relationship to the rejected mirror-role approach

Model-centric endpoints are the systemic version of the rejected `implement-overflow-cc` band-aid without making provider subscriptions into role contracts. Roles remain capability contracts. Subscription class and provider identity become endpoint metadata resolved inside the allocator, so role names do not leak vendors or topology and one logical model can have ordered reserves. Mirror catalog keys preserve the one-to-one parser shape but recreate the coupling and duplication that the endpoint model is intended to remove.

## Cross-repository scope

`grok-build` is canonical. Porting this design or its eventual implementation to the live dotfiles/`.grok` tree is a separate step and is not part of this note.

## Non-goals

This design is not adaptive topology (R7), an ML scheduler, or generic round-robin load balancing. It is failover-first: preserve the selected logical model and try ordered endpoints before moving through the tier ladder. It makes no enforcement promises beyond the cooperative routing contract.
