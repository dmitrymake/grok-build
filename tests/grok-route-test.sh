#!/usr/bin/env bash
set -euo pipefail

# Grok Build regressions: classifier plus A+Stop and soft-deny hook behavior.
# A live ~/.grok tree may symlink here, so always run the checkout copy.

cd "$(dirname "$0")/.."

GROK_HOME="$(mktemp -d)"
XDG_SANDBOX="$(mktemp -d)"
export GROK_HOME
export XDG_STATE_HOME="$XDG_SANDBOX/state"
export XDG_CACHE_HOME="$XDG_SANDBOX/cache"
mkdir -p "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
# CI is hermetic by design: seed the isolated home from this checkout, never the account.
cp config/config.toml "$GROK_HOME/config.toml"
cp -R agents "$GROK_HOME/agents"
trap 'rm -rf "$GROK_HOME" "$XDG_SANDBOX"' EXIT

python3 tests/test_classify.py
python3 tests/test_role_sets.py
python3 tests/test_tier_matrix.py
# test_route.py builds synthetic feedback from the live gate recipe shapes.
python3 tests/test_route.py
python3 tests/test_visual_intake.py
python3 tests/test_visual_cache.py
python3 tests/test_router.py
python3 tests/test_acceptance.py
python3 tests/test_state_machine.py
python3 tests/test_security_regressions.py
python3 tests/test_task_payload_contracts.py
python3 tests/test_pipeline_tiers.py
python3 tests/test_selector_v2_demo.py
python3 tests/test_standalone_registration.py
python3 tests/test_discovery.py
python3 tests/test_subscription_class.py
python3 tests/test_workloads.py
python3 tests/test_corpus_sync.py
python3 tests/test_verify_debt_repro.py
python3 tests/test_eval_composition.py
python3 tests/test_eval_run_record.py
PYTHONPATH=. python3 tests/test_hook_hardening_followup.py
PYTHONPATH=. python3 tests/test_panel_followup.py
PYTHONPATH=. python3 tests/test_provider_hygiene_followup.py
PYTHONPATH=. python3 tests/test_security_regressions_followup.py
PYTHONPATH=. python3 tests/test_spawn_capability_followup.py
PYTHONPATH=. python3 tests/test_turn_fingerprint_followup.py
python3 -m grokbuild.render_docs check --config config/config.toml --providers grokbuild/providers.json --intents grokbuild/intents.json --prose grokbuild/contract_prose.json
