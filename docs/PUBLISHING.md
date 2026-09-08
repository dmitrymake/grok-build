# Publishing

The repository history retains earlier private term-list revisions, and the history scan excludes the term-list paths and historical consumers. Publish from a fresh-init export rather than rewriting the working repository:

1. Start from a green working tree and export tracked files into a new release directory.
2. In the release directory, run `git init`, add only the export, and create one fresh public commit.
3. Run `scripts/release-check.sh` in the fresh repository and record the source commit and tree digest in the release notes. Do not copy refs, tags, or reflogs.

## Never ship

- `approve.json`, `approve_owner.json`, `deposit.json`, `deposit_owner.json`, `fund_strategy.json`, `guardian_update.json`, `loss_transfer.json`, `owner_setkeyring.json`, or `stop.json`
- `*.local.txt`
- `dist/`
- `__pycache__/`
- `bench/runs/`
- `bench/tb-runs/`

## Pre-push checklist

- `./scripts/release-check.sh` is green.
- `python -m pytest tests/ -q` is green.
- `./tests/grok-route-test.sh` is green.
- `./tests/grok-combine-install-test.sh` is green.
- Manually grep the export for all nine receipt filenames and `local.txt`.
- Confirm the config terms are neutralized generic project/workload labels.
