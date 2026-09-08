# Cheap advisors: risk-recon design note

## Context and incident value

The risk-recon pattern, called **DO** in this note, adds a narrowly scoped read-only advisor to the recon barrier. The advisor inspects existing reconnaissance and delegation context before implementation and reports only risks that can be checked immediately. It does not replace normal reconnaissance, choose an implementation tier, or authorize work.

The motivating incident class is a plausible implementation path that looks complete but misses a cheap, testable risk visible in repository evidence. A bounded independent lens can identify such a risk before writable work begins without turning the advisor into a planner or adding an unbounded review stage. Its value must be established by the offline and later paired evaluation arms below; this note does not claim outcome improvement in advance.

## Normative risk-flag format

The advisor **MUST** emit at most five flags. Each flag **MUST** contain exactly the following semantic fields:

- `risk`: a concise statement of the failure or regression risk;
- `evidence`: a repository `file:line` reference supporting the risk;
- `check`: one verifiable check that takes no more than one minute;
- `confidence`: the advisor's calibrated confidence in the flag.

A flag without a verifiable `check` is **INVALID**. A check that cannot reasonably complete within one minute is invalid for this lens. `No flags` is a valid answer and must not be converted into a synthetic concern. Flags are advisory evidence, not permission to edit, skip a required stage, or weaken a gate.

## Normative profile scoping and controller amendment

The risk-recon lens is **PROFILE-SCOPED, NOT a position in the general recon lens pool**.

This is required because the general pool cannot simultaneously provide both properties the experiment needs. Default-profile inertness would require a late pool position of at least six, while the width-three swap arm would require an early position of at most three. At high complexity, the default recon width grows to five (`grokbuild/pipeline.py`, high-complexity recon-width selection), so placing the lens at pool positions four or five would leak it into default production composition.

Therefore the `default` profile contains no advisor, while the `advisor` profile names the advisor role. Production resolves the advisor separately from ordinary recon-pool slicing. The invariant that default composition is unchanged holds **by construction**, rather than by relying on a width that may change.

## Role contract

The stable role name is `explore-risk`. It is read-only and uses `permission_mode: plan`. Its live model binding is `minimax-m3`; the repository configuration uses the sanitized equivalent pin following the existing `visual-intake` convention. The role inspects only the supplied recon and delegation context, emits the bounded flag format above, and never edits files.

Availability is part of the composition contract. If the role's configured model or credential is unavailable, production omits this optional lens and emits degraded-composition evidence. It must not hold the recon barrier until the barrier-stall timeout.

## R1 ablation arms and stable names

These arms are R9 recon-composition transformations and retain the same task, controls, implementation stages, and verifier timing:

- **A0 — `full-pipeline`:** the existing baseline/default-equivalent row. Existing variants are not renamed, and no advisor is added.
- **A1 — `recon-risk-swap`:** at recon width three, deterministically replace one ordinary recon member with one `explore-risk` member. Total recon member count and `recon/0..N-1` IDs remain unchanged.
- **A2 — `recon-risk-additive`:** append one `explore-risk` member after all base recon members. Existing member order and IDs remain unchanged; the advisor receives the next deterministic recon member ID.

A1 isolates lens substitution at a fixed width. A2 measures the additional lens and its extra member cost. A0 is the existing full-pipeline/default equivalence and remains the production-default comparison.

## Production wiring plan

Production wiring supports only the additive form. The ordinary recon pool remains unchanged. When and only when the selected profile is `advisor`, production resolves the profile's advisor role, checks availability, and appends it to the recon barrier. The swap form exists only as an offline evaluation arm.

If the advisor is unavailable, production composes the same recon barrier it would have composed without the advisor, records a degraded-recon warning, and continues immediately. Advisor absence never delays the barrier and never changes default-profile behavior.

## Telemetry and evidence notes

Composition evidence should record the selected profile, advisor role, availability outcome, member ID, and whether omission was caused by unavailable binding or credentials. Risk flags should preserve the bounded structured fields and repository evidence reference. Offline synthetic accounting remains zero-token and must count no provider calls or tokens.

This foundation does not change the state-v8 serializer, log-v4 telemetry generation, or existing default-route decisions. Any future live evaluation must record provider, model, effort, resolved availability, wall-clock cost, token usage, and paired arm identity through the existing run-record boundary rather than infer them from role names.

## Migration and tests

Migration is additive: introduce the role and the separate `advisor` profile field, leave the `default` profile unchanged, and append the role only after a successful availability check. Deterministic tests must cover exact variant names, member roles and IDs, fixed-width swap cardinality, additive cardinality, registry binding, R9 validation, zero synthetic accounting, default medium/high inertness, advisor availability, and immediate unavailable-role degradation. Production modules must not import `eval`.

## Open questions

1. Which paired task classes show enough risk detection benefit to justify the additive member cost?
2. What confidence representation and calibration threshold should later live evaluation standardize without suppressing valid `No flags` results?
3. Should invalid flags be discarded individually or invalidate the complete advisor result?
4. What bounded telemetry shape can retain useful flag evidence without changing state-v8 or log-v4 compatibility?

## Non-goals

- No planner market.
- No frontier escalation mechanism.
- No learned router or adaptive topology.
- No replacement of required reconnaissance or review stages.
- No authority to edit files, approve risky work, or bypass gates.
- No claim that the advisor improves live outcomes before paired evidence exists.
