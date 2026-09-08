---
name: corpus-sync
description: Harvest, review, and merge the local routing workload corpus; use for corpus, workloads, dataset, датасет задач, drift, or harvest.
when-to-use: corpus, workloads, dataset, task dataset, prompt corpus, датасет задач, drift, harvest
user-invocable: true
---

# Sync the routing workload corpus

Run `/corpus-sync harvest` before every `/rebuild-stack` re-pin. The workflow is
local-only and keeps prompts redacted:

1. `harvest` reads local route telemetry and session transcripts, then writes
   candidates to `~/.local/state/grok-route/workloads-staging.json` (or the
   configured staging path) and advances the watermark at
   `~/.local/state/grok-route/corpus-sync.json`. A legacy repository staging
   file is migrated once and deleted.
2. Run `triage` (default top 50, or `--limit`) for a priority-sorted batch
   review. Edit `expect` directly in the state-directory staging file.
3. **REVIEW staging manually.** Fill `expect` by judgment: `observed` is what
   the router does TODAY, while `expect` is what it SHOULD do. Never copy
   observed into expect blindly. `drift:<id>` entries mean either the
   classification changed or the corpus contract drifted and require review.
4. `merge` moves only manually adjudicated entries with a non-null expected
   intent into `fixtures/workloads.json`.
5. Run `./tests/grok-route-test.sh` and
   `./tests/grok-combine-install-test.sh`.

Staging is capped at 500 entries; eviction removes the oldest lowest-priority
items and records their hashes in the bounded rejection set. Use `status` to
inspect the watermark, staging flag counts, and corpus size.
The dataset and telemetry remain on the local machine; harvested prompt text is
collapsed, truncated, and passed through the shared redactor. Expectations are
never auto-filled from observed routing.

## Import real history

Run `/corpus-sync import-history` to scan supported prompt-history and project
session JSONL files (top-level files only). It tracks only file size/mtime and a bounded normalized prompt-hash set:
unchanged files are skipped, while changed, rotated, or replaced files are fully
rescanned. Use `--since`, `--limit`,
`--min-chars`, `--workspaces`, or `--no-claude` to bound the import.

The ordered provenance classifier never stages automation metadata, wrappers,
commands, child briefs, or generated task briefs. Explicitly human prompts are
staged; weak textual human classification is limited to short
Cyrillic-dominant Grok history. Uncertain prompts require a local commit within
48 hours or `--keep-uncertain`. Staging records `origin`, `origin_rule`, and any
`git_evidence`; reports contain counts, never prompt text.

Run `/corpus-sync import-git` to mine non-chore commits from local workspace
logs. Git-derived tasks are the high-precision backbone and are watermarked by
workspace and SHA; the command never contacts remotes. The
`observed` object remains the static router's verdict TODAY. Adjudicating
`expect` is always manual before `merge`.
