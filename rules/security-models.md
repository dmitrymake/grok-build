# Grok Build routing contract

<!-- grok-build:generated begin id=security-contract schema=1 -->
The conductor follows `glm-5.3-flash → gemini-3.7-flash → grok-4.6`, starts at `glm-5.3-flash` with global `high` effort, remains zero-write, and uses `gemini-3.7-flash` for advisory opinions below 0.3 confidence. Web research stays on `grok-4.6`.

| Job role | Model @ effort | Autonomy | Contract |
| --- | --- | --- | --- |
| `consilium-analyst` | `glm-5.3 @ max [broad]` | `broad` | fresh-eyes read-only failure analysis from telemetry and state |
| `consilium-arbiter` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | read-only synthesis of a repair verdict and concrete plan |
| `consilium-challenger` | `deepseek-v4-pro @ high [standard]` | `standard` | adversarial read-only cross-examination of repair attempts |
| `criterion-judge` | `glm-5.3-flash @ high [standard]` | `standard` | calibrated forced-choice criterion token from provider logits; unavailable logits abstain |
| `expert-rescue` | `deepseek-v4-pro @ high [standard]` | `standard` | independent read-only diagnosis; never implements |
| `explore` | `gpt-5.6-luna @ high [standard]` | `standard` | scoped read-only reconnaissance |
| `explore-risk` | `minimax-m3 @ high [standard]` | `standard` | profile-scoped read-only risk reconnaissance with bounded verifiable flags |
| `explore-thorough` | `gpt-5.6-luna @ max [standard]` | `standard` | architecture-wide read-only reconnaissance |
| `frontier-resolver` | `grok-4.6 [standard]` | `standard` | rare meta-planning escalation for genuinely ambiguous tasks |
| `frontier-resolver-standby` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | standby meta-planning escalation when the primary frontier binding is unavailable |
| `implement` / `implement-standard` | `gpt-5.6-luna @ max [guided]` | `guided` | default implementation alias; exact pair with implement-standard |
| `implement-cheap` | `gpt-5.6-luna @ high [guided]` | `guided` | low-scope implementation; effort-distinct from standard |
| `implement-cheap-fallback` | `deepseek-v4-flash @ high [guided]` | `guided` | strict last resort: low-risk, testable work with a known verifier |
| `implement-hard` | `gpt-6-astra @ max [broad]` | `broad` | hard implementation |
| `implement-ops` | `glm-5.3 @ high [standard]` | `standard` | firmware, operations, destructive, or physical implementation |
| `implement-overflow` | `deepseek-v4-pro @ high [standard]` | `standard` | cross-provider overflow after primary tiers fail |
| `implement-strong` | `gpt-5.6-sol @ max [standard]` | `standard` | strong middle-tier implementation within the declared scope |
| `judge-challenger-agentic` | `gpt-oss-120b @ low [standard]` | `standard` | opt-in read-only agentic challenger via Together AI; composes only under the judge-challengers profile |
| `judge-challenger-structural` | `gpt-oss-120b @ low [standard]` | `standard` | opt-in read-only structural challenger via Together AI; composes only under the judge-challengers profile |
| `judge-disagreement` | `gpt-5.6-luna @ high [standard]` | `standard` | adjudicates only when the two cheap judges disagree after a discriminating test |
| `judge-frontier-code` | `grok-4.6 [standard]` | `standard` | frontier adjudication for code artifacts, bought only after cheaper comparison fails to resolve |
| `judge-frontier-general` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | frontier adjudication for non-code artifacts, bought only after cheaper comparison fails to resolve |
| `judge-independent` | `glm-5.3-flash @ high [standard]` | `standard` | second independent comparison of the same pair in reversed order, from a different provider |
| `judge-primary` | `gemini-3.7-flash @ high [standard]` | `standard` | compares candidate artifacts pairwise on evidence |
| `plan` / `plan-hard` | `gpt-6-astra @ max [broad]` | `broad` | read-only planning alias; exact pair with plan-hard |
| `plan-comparator` | `gemini-3.7-flash @ high [standard]` | `standard` | agreement signal across competing decompositions; never selects a plan |
| `planner-a` | `deepseek-v4-pro @ high [standard]` | `standard` | independent read-only decomposition (primary provider) |
| `planner-b` | `glm-5.3 @ high [broad]` | `broad` | independent read-only decomposition (redundant endpoint, same family) |
| `planner-c` | `gpt-5.6-luna @ high [standard]` | `standard` | independent read-only decomposition (different model family) |
| `planner-strong` | `gpt-5.6-sol @ max [standard]` | `standard` | optional strong read-only planner seat for the expanded market |
| `researcher` | `gpt-5.6-sol @ max [broad]` | `broad` | deep multi-source research and synthesis lead |
| `researcher-analyst` | `glm-5.3 @ max [broad]` | `broad` | alternative-perspective research analysis |
| `researcher-challenger` | `grok-4.6 [standard]` | `standard` | adversarial verification of research findings |
| `review` | `gpt-5.6-sol @ xhigh [standard]` | `standard` | legacy alias; prefer review-hard |
| `review-hard` | `glm-5.3 @ max [standard]` | `standard` | internal read-only review |
| `review-independent` | `grok-4.6 [standard]` | `standard` | independent read-only review with an explicit availability signal |
| `security` | `glm-5.3 @ max [standard]` | `standard` | read-only security analysis |
| `security-verify` | `grok-4.6 [standard]` | `standard` | independent read-only security verification with an explicit availability signal |
| `verifier-planner` | `glm-5.3-flash @ high [standard]` | `standard` | states what evidence would settle an undecided comparison |
| `visual-intake` | `minimax-m3 [standard]` | `standard` | read-only schema-v1 image preflight; explicit vision capability required |
| `visual-intake-deep` | `gpt-5.6-terra [standard]` | `standard` | read-only high-detail fallback and targeted reinspection |
| `general-purpose` | `gpt-5.6-luna` | infrastructure-child compatibility pin |

Provider pool A supplies `glm-5.3`, `glm-5.3-flash` via `ZAI_API_KEY`.
Grok roles use the official `grok login` session with no model credential binding.
Proxy-backed models use a configured proxy endpoint and API key.
Batch models `glm-5.3-flash` and `deepseek-v4-flash` handle mass text, extraction, classification workloads.
<!-- grok-build:generated end id=security-contract schema=1 -->

## Visual intake lifecycle

The TUI detects image bytes before conductor invocation. It normalizes opaque attachment IDs and content hashes, invokes `visual-intake`, validates schema v1, and gives the conductor compact auxiliary context while retaining the original IDs. `visual-intake-deep` handles comparisons, dense detail, low confidence, and explicit re-analysis. Text-only turns remain unchanged. Visual preflight is not an intent, does not enter `Pipeline.compose_execution()`, and never makes a role writable. Failures produce a typed conductor marker. Cache entries contain validated structured results only and are invalidated by schema, role, options, attachment order, and binding version.

## Delegation contract

1. Every implementation starts with delegated reconnaissance: one linear
   `explore` for low complexity, the Stage C explore barrier for medium/high.
   Until recon finishes, the conductor must not map the tree; tight `read_file`
   reads for the child prompt remain allowed.
2. Finish each required stage before edits. The conductor remains zero-write
   after completion; only implementation children edit. It may run only an exact
   configured deterministic verifier when due.
3. Medium/high implementation always composes `review-panel` or `review-hard`,
   regardless of tier. Missing review falls back with a warning to the exact
   configured deterministic verifier; without one, the route is observe-only.
4. Plan/ADR work finishes `plan-hard` before a writable delegated role writes
   the artifact. `/plan` alone is not completion evidence.
5. `expert-rescue` diagnoses only; an implementation role makes changes.
6. Missing, failed, circuit-open, or unspawnable roles keep their stage failed
   and gate active. Phrase-only implement is profile-gated; phrase-only security
   remains observe-only. Child sessions are gate-exempt.
7. Track every parallel member by barrier key and every bound task by id,
   including correlated background terminal jobs. A failed child spawn,
   verifier, or bound-task retrieval reopens its stage and reactivates the gate.
8. Repair failed stages in the same turn or report the blocking cause. Child
   stations signal failure by starting the final message with `BLOCKED:`; the
   conductor Stop hook detects it without waiting for retrieval. Unfinished
   execution debt survives turns for the bounded session window and blocks
   `end_turn` until repaired or the bounded Stop limit is reached.
9. A repair stage reaching `consilium_after_failures` consecutive failures
   convenes three cross-provider read-only diagnosticians; an implementation
   tier applies the arbiter plan.

Verification assumes the artifact is reachable. For external/sandbox tasks (docker-exec, remote wrappers), a read-only reviewer may be unable to inspect the artifact directly; in that case the reviewer's BLOCKED 'cannot verify' report is treated as review_inconclusive, not a failed review: the stage is discharged and the harness accepts a static conclusion based on the typed evidence already gathered. A deterministic task verifier, when the task defines one, is executed by a writable child — never by the zero-write conductor.
