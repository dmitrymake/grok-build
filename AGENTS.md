# Repository agent guide

<!-- grok-build:generated begin id=agents-contract schema=1 -->
- Stable job-role names are the public contract; vendor- or model-named roles are forbidden.
- Briefing autonomy is launch-time policy: guided roles receive complete precise briefs, standard roles receive goals and constraints with bounded discretion, and broad roles may self-direct read-only investigation within scope.
- When a child transcript approaches 80% of its context window, resume or respawn preserving its read-only/writable class; prefer gpt-6-astra (1.05M), then glm-5.3.
- Role pins and efforts live only in `config/config.toml`; recipes render as model @ effort. The built-in `general-purpose` role is pinned through `[subagents.models]`.
- The conductor fallback chain is `glm-5.3-flash → gemini-3.7-flash → grok-4.6`.
- Provider pool A models (`glm-5.3`, `glm-5.3-flash`) use the configured `ZAI_API_KEY` credential.
- The conductor is permanently zero-write; only identified implementation children may write, and they must use `capability_mode="all"`.
- Delegated reconnaissance precedes medium/high implementation; the conductor does not map the repository.
- Medium/high implementation finishes a review stage or, when review is unavailable, the exact configured deterministic verifier with a warning; without either, the route degrades observe-only.
- A failed child spawn, verifier, or bound background job retrieval reopens its stage; re-engage in the same turn or report `BLOCKED` with the failure cause.
- A stage failing `consilium_after_failures` consecutive repairs convenes a read-only cross-provider consilium barrier; an implementation tier still applies the fix.
- Unfinished execution debt survives turn boundaries within the bounded session window and blocks `end_turn` until repaired or the bounded Stop limit is reached.
- Only `kill_command_or_subagent` is a conductor lifecycle exception; barrier stalls remain observe-only.
- Media intake roles are semantic, and vision capability must be explicit in the provider catalog; visual preflight is not an intent or execution stage.
- Never commit, print, copy, or delete secrets, `~/.env_keys`, or OAuth files.
- For Grok routing changes run `./tests/grok-route-test.sh` and `./tests/grok-combine-install-test.sh`.
<!-- grok-build:generated end id=agents-contract schema=1 -->
