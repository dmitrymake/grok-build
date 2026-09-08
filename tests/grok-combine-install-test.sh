#!/usr/bin/env bash
set -euo pipefail

# Public installer contract. All installation runs use isolated temporary homes.

cd "$(dirname "$0")/.."

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

bash -n scripts/install.sh || fail "syntax scripts/install.sh"
bash -n scripts/grok-route || fail "syntax scripts/grok-route"
[ -x hooks/route.py ] && [ -x grokbuild/cli.py ] && [ -x grokbuild/conductor.py ] \
  && [ -x scripts/install.sh ] && [ -x scripts/grok-route ] \
  && [ -x tests/grok-route-test.sh ] && [ -x tests/grok-combine-install-test.sh ] \
  || fail "public scripts must be executable"
grep -Eq '"command": "(GROK_HOOK_EVENT=[A-Za-z]+ )?\./route\.py"' hooks/route.json \
  || fail "route.json must invoke its adjacent launcher"
! grep -q '/home/' hooks/route.json || fail "route.json must not hardcode a home directory"

python3 - <<'PY' || fail "repository config must preserve the model and role matrix"
import tomllib
with open("config/config.toml", "rb") as stream:
    data = tomllib.load(stream)
assert data["models"]["default"] == "glm-5.3-flash"
assert data["model"]["glm-5.3-flash"]["base_url"] == "https://api.z.ai/api/coding/paas/v4"
assert data["subagents"]["models"]["general-purpose"] == "gpt-5.6-luna"
assert data["subagents"]["roles"]["implement-overflow"]["model"] == "deepseek-v4-pro"
PY

XDG_SANDBOX="$(mktemp -d)"
export XDG_STATE_HOME="$XDG_SANDBOX/state"
export XDG_CACHE_HOME="$XDG_SANDBOX/cache"
mkdir -p "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
fake="$(mktemp -d)"
fake2="$(mktemp -d)"
fake3="$(mktemp -d)"
fake_partial="$(mktemp -d)"
fake_semantic="$(mktemp -d)"
fake_duplicate="$(mktemp -d)"
trap 'rm -rf "$fake" "$fake2" "$fake3" "$fake_partial" "$fake_semantic" "$fake_duplicate" "$XDG_SANDBOX"' EXIT
unset XDG_RUNTIME_DIR || true

# First install: links point to the checkout and the live config validates.
export HOME="$fake/home"
mkdir -p "$HOME"
REPO_DIR="$PWD" bash scripts/install.sh >"$fake/install.log" 2>&1 || {
  cat "$fake/install.log" >&2; fail "first install failed";
}
[ -f "$HOME/.grok/config.toml" ] && [ ! -L "$HOME/.grok/config.toml" ] \
  || fail "expected account-owned regular live config"
for path in hooks/route.py hooks/route.json routing/providers.json routing/barrier_lenses.json routing/conductor.py \
  routing/payloads.py routing/stats.py routing/discovery.py routing/visual_intake.py \
  routing/visual_cache.py routing/corpus_sync.py routing/fixtures/workloads.json \
  rules/security-models.md; do
  [ -L "$HOME/.grok/$path" ] || fail "expected live symlink ~/.grok/$path"
done
[ "$(readlink "$HOME/.grok/hooks/route.py")" = "$PWD/hooks/route.py" ] || fail "hook source mismatch"
[ "$(readlink "$HOME/.grok/routing/providers.json")" = "$PWD/grokbuild/providers.json" ] || fail "routing source mismatch"
printf '{}' | "$HOME/.grok/hooks/route.py" >/dev/null || fail "installed hook smoke failed"

python3 - "$HOME/.grok/routing/providers.json" "$HOME/.grok/config.toml" "$HOME/.grok/routing/barrier_lenses.json" <<'PY' || fail "installed data validation failed"
import json, sys, tomllib
from pathlib import Path
import types
routing = Path(sys.argv[3]).parent
package = types.ModuleType("grokbuild")
package.__path__ = [str(routing)]
sys.modules["grokbuild"] = package
from grokbuild.compose import compose_execution, load_barrier_lenses
providers = json.load(open(sys.argv[1]))
models = {mid: spec for p in providers["providers"].values() for mid, spec in p["models"].items()}
assert providers["version"] == 2
assert models["minimax-m3"]["capabilities"]["vision"] is True
assert models["gpt-5.6-terra"]["capabilities"]["vision"] is True
with open(sys.argv[2], "rb") as stream:
    data = tomllib.load(stream)
roles = data["subagents"]["roles"]
assert data["models"]["default"] == "glm-5.3-flash"
assert data["models"]["default_reasoning_effort"] == "high"
assert roles["implement-hard"]["model"] == "gpt-6-astra"
assert roles["implement-hard"]["reasoning_effort"] == "max"
assert roles["security-verify"]["model"] == "grok-4.6"
assert data["visual_intake"]["schema_version"] == 1
assert "ui" not in data
assert "permission_mode" not in Path(sys.argv[2]).read_text(encoding="utf-8")
assert Path(sys.argv[3]).is_symlink() or Path(sys.argv[3]).is_file()
assert load_barrier_lenses(sys.argv[3])["research"]

without_challenger = compose_execution("research", "high", "low", "researcher", None)
with_challenger = compose_execution(
    "research", "high", "low", "researcher", None, researcher_challenger_ok=True
)
assert len(without_challenger[0].members) == 4
assert len(with_challenger[0].members) == 5
assert "researcher-challenger" in {member.role for member in with_challenger[0].members}
PY

for agent in implement review security implement-cheap implement-cheap-fallback \
  implement-standard implement-strong implement-hard implement-ops implement-overflow explore-thorough explore-risk \
  review-hard review-independent security-verify plan-hard consilium-analyst \
  consilium-challenger consilium-arbiter researcher researcher-analyst researcher-challenger \
  planner-a planner-b planner-c planner-strong plan-comparator \
  criterion-judge judge-primary judge-independent judge-disagreement judge-frontier-code judge-frontier-general \
  verifier-planner frontier-resolver frontier-resolver-standby visual-intake visual-intake-deep; do
  [ -L "$HOME/.grok/agents/$agent.md" ] || fail "missing agent $agent"
  [ "$(readlink "$HOME/.grok/agents/$agent.md")" = "$PWD/agents/$agent.md" ] || fail "agent source mismatch: $agent"
  ! grep -q '^model:' "$HOME/.grok/agents/$agent.md" || fail "agent $agent must not pin a model"
done
for skill in implement model-routing rebuild-stack security corpus-sync bounty-triage; do
  [ -L "$HOME/.grok/skills/$skill/SKILL.md" ] || fail "missing skill $skill"
done
python3 - "$HOME/.grok/grok-roles-manifest.json" "$PWD" <<'PY' || fail "roles manifest invalid"
import json, sys
manifest = json.load(open(sys.argv[1]))
want = {"implement", "review", "security", "implement-cheap", "implement-cheap-fallback",
        "implement-standard", "implement-strong", "implement-hard", "implement-ops", "implement-overflow", "planner-strong",
        "explore-thorough", "explore-risk", "review-hard", "review-independent", "security-verify", "plan-hard",
        "consilium-analyst", "consilium-challenger", "consilium-arbiter", "researcher", "researcher-analyst", "researcher-challenger", "criterion-judge", "visual-intake", "visual-intake-deep"}
assert want <= {item["name"] for item in manifest["roles"]}
assert all(item["source"].startswith(sys.argv[2] + "/agents/") for item in manifest["roles"])
PY

# Second run is idempotent and does not create a config backup.
cp "$HOME/.grok/config.toml" "$fake/config.first"
REPO_DIR="$PWD" bash scripts/install.sh >"$fake/install2.log" 2>&1 || fail "second install failed"
cmp -s "$HOME/.grok/config.toml" "$fake/config.first" || fail "regular config changed on second install"
! find "$HOME/.grok" -maxdepth 1 -name 'config.toml.before-combine-*.bak' | grep -q . || fail "fresh config created a backup"

# A checkout path containing spaces remains usable.
space_dir="$fake/with space/grok-build"
mkdir -p "$space_dir"
for entry in grokbuild hooks config agents skills rules scripts tests; do
  ln -s "$PWD/$entry" "$space_dir/$entry"
done
space_home="$fake/space-home"
mkdir -p "$space_home"
HOME="$space_home" REPO_DIR="$space_dir" bash scripts/install.sh >"$fake/install-space.log" 2>&1 || {
  cat "$fake/install-space.log" >&2; fail "spaced-checkout install failed";
}
[ "$(readlink "$space_home/.grok/hooks/route.py")" = "$space_dir/hooks/route.py" ] || fail "spaced hook source mismatch"
printf '{}' | "$space_home/.grok/hooks/route.py" >/dev/null || fail "spaced-checkout hook smoke failed"

# A regular account config preserves unrelated tables and receives all managed tables.
export HOME="$fake2/home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[marketplace]
default_skills_installs_purged = false

[cli]
auto_update = true

[models]
default = "deepseek-v4-pro"

[model."gpt-5.6-sol"]
model = "gpt-5.6-sol"
api_key = "old-value"

[privacy]
privacy_banner_acked = "test"
EOF
REPO_DIR="$PWD" bash scripts/install.sh >"$fake2/install.log" 2>&1 || {
  cat "$fake2/install.log" >&2; fail "regular-config install failed";
}
[ -f "$HOME/.grok/config.toml" ] && [ ! -L "$HOME/.grok/config.toml" ] || fail "regular config was replaced"
grep -q '^auto_update = true$' "$HOME/.grok/config.toml" || fail "CLI table was not preserved"
grep -q '^privacy_banner_acked = "test"$' "$HOME/.grok/config.toml" || fail "privacy table was not preserved"
! grep -q '^\[ui\]$' "$HOME/.grok/config.toml" || fail "account config gained repository UI preferences"
grep -q 'api_key = "old-value"' "$HOME/.grok/config.toml" || fail "live managed model was overwritten"
ls "$HOME/.grok"/config.toml.before-combine-*.bak >/dev/null 2>&1 || fail "regular config was not backed up"
[ -L "$HOME/.grok/hooks/route.py" ] || fail "valid merged config did not install hooks"
python3 - "$HOME/.grok/config.toml" <<'PY' || fail "merged config tables invalid"
import sys, tomllib
with open(sys.argv[1], "rb") as stream:
    data = tomllib.load(stream)
assert data["models"]["default"] == "deepseek-v4-pro"
assert data["routing"]["verifiers"]["$REPO_ROOT"] == ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]
assert data["routing"]["conductor"] == {
    "default": "glm-5.3-flash", "fallback": "gemini-3.7-flash", "emergency": "grok-4.6",
    "candidates": ["glm-5.3-flash", "gemini-3.7-flash", "grok-4.6"],
}
assert data["routing"]["second_opinion"] == {"model": "gemini-3.7-flash", "provider": "commandcode"}
assert set(data["corpus"]["taxonomy"]) == {"analytics", "fw", "df", "sec", "gb"}
PY

# Existing verifier and partial routing/corpus tables preserve custom values and gain missing keys.
export HOME="$fake_partial/home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[routing.verifiers]
"/home/someone/other-repo" = ["./other.sh"]

[routing.second_opinion]
model = "custom-model"

[corpus.taxonomy]
data = ["custom-analytics"]
EOF
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_partial/install.log" 2>&1 || fail "partial-table install failed"
python3 - "$HOME/.grok/config.toml" <<'PY' || fail "partial tables were not merged correctly"
import sys, tomllib
with open(sys.argv[1], "rb") as stream:
    data = tomllib.load(stream)
assert data["routing"]["verifiers"]["/home/someone/other-repo"] == ["./other.sh"]
assert data["routing"]["verifiers"]["$REPO_ROOT"] == ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]
assert data["routing"]["second_opinion"] == {"model": "custom-model", "provider": "commandcode"}
assert data["corpus"]["taxonomy"]["data"] == ["custom-analytics"]
assert set(data["corpus"]["taxonomy"]) == {"data", "analytics", "fw", "df", "sec", "gb"}
assert "conductor" in data["routing"]
PY
cp "$HOME/.grok/config.toml" "$fake_partial/config.first"
backups_before="$(find "$HOME/.grok" -maxdepth 1 -name 'config.toml.before-combine-*.bak' | wc -l)"
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_partial/install2.log" 2>&1 || fail "second partial-table install failed"
cmp -s "$HOME/.grok/config.toml" "$fake_partial/config.first" || fail "second partial install changed config"
backups_after="$(find "$HOME/.grok" -maxdepth 1 -name 'config.toml.before-combine-*.bak' | wc -l)"
[ "$backups_after" -eq "$backups_before" ] || fail "second partial install created another backup"

# A moved, incomplete checkout removes only stale repository-owned hook links.
export HOME="$fake3/home"
mkdir -p "$HOME"
REPO_DIR="$PWD" bash scripts/install.sh >"$fake3/install-valid.log" 2>&1 || fail "initial moved-checkout setup failed"
[ -L "$HOME/.grok/hooks/route.py" ] || fail "initial moved-checkout hook missing"
broken="$fake3/broken-grok-build"
mkdir -p "$broken"
for entry in grokbuild hooks config rules scripts tests; do ln -s "$PWD/$entry" "$broken/$entry"; done
rm -f "$HOME/.grok/config.toml" "$HOME/.grok/agents/"*.md
cat > "$HOME/.grok/config.toml" <<'EOF'
[models]
default = "deepseek-v4-pro"
EOF
renamed="$fake3/release-tarball-0.1.0"
mkdir -p "$renamed/hooks"
ln -s "$PWD/hooks/route.py" "$renamed/hooks/route.py"
ln -sfn "$renamed/hooks/route.py" "$HOME/.grok/hooks/route.py"
ln -sfn "/tmp/old/grok-build/hooks/route.json" "$HOME/.grok/hooks/route.json"
echo unrelated > "$HOME/.grok/hooks/keep.txt"
ln -sfn /tmp/foreign-target "$HOME/.grok/hooks/keep.py"
REPO_DIR="$broken" bash scripts/install.sh >"$fake3/install-upgrade.log" 2>&1 || {
  cat "$fake3/install-upgrade.log" >&2; fail "broken-checkout upgrade aborted";
}
[ ! -L "$HOME/.grok/hooks/route.py" ] || fail "stale route.py link survived"
[ ! -L "$HOME/.grok/hooks/route.json" ] || fail "stale route.json link survived"
[ "$(cat "$HOME/.grok/hooks/keep.txt")" = unrelated ] || fail "unrelated regular hook changed"
[ -L "$HOME/.grok/hooks/keep.py" ] || fail "foreign hook symlink changed"
grep -q 'removed stale repository hook link' "$fake3/install-upgrade.log" || fail "stale-hook removal was not diagnosed"

ln -sfn "/tmp/h11-random/hooks/route.json" "$HOME/.grok/hooks/route.json"
REPO_DIR="$broken" bash scripts/install.sh >"$fake3/install-random.log" 2>&1 || {
  cat "$fake3/install-random.log" >&2; fail "random stale-hook upgrade aborted";
}
[ ! -L "$HOME/.grok/hooks/route.json" ] || fail "random stale route.json link survived"

mkdir -p "$HOME/.grok/archive/hooks"
ln -sfn "$HOME/.grok/archive/hooks/route.py" "$HOME/.grok/hooks/route.py"
REPO_DIR="$broken" bash scripts/install.sh >"$fake3/install-internal.log" 2>&1 || {
  cat "$fake3/install-internal.log" >&2; fail "internal-hook upgrade aborted";
}
[ -L "$HOME/.grok/hooks/route.py" ] || fail "internal route.py link was removed"

# TOML-equivalent quoted and bare model headers are treated as the same table.
export HOME="$fake_semantic/home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[models]
default = "live-default"

[model.gpt-6-astra]
model = "live-astra"
base_url = "http://live-pin"

[model.gpt-oss-120b]
model = "live-oss"

[routing."conductor"]
default = "live-conductor"

[routing."verifiers"]
"$REPO_ROOT" = ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]

[routing."second_opinion"]
model = "live-opinion"

[corpus."taxonomy"]
version = "live-taxonomy"
EOF
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_semantic/install.log" 2>&1 || {
  cat "$fake_semantic/install.log" >&2; fail "semantic-header install failed";
}
python3 - "$HOME/.grok/config.toml" <<'PY' || fail "semantic table merge was not preserved"
import sys, tomllib
from pathlib import Path
path = Path(sys.argv[1])
data = tomllib.loads(path.read_text(encoding="utf-8"))
assert data["models"]["default"] == "live-default"
assert data["model"]["gpt-6-astra"] == {"model": "live-astra", "base_url": "http://live-pin"}
assert data["model"]["gpt-oss-120b"] == {"model": "live-oss"}
assert data["routing"]["conductor"]["default"] == "live-conductor"
assert data["routing"]["verifiers"]["$REPO_ROOT"] == ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]
assert data["routing"]["second_opinion"]["model"] == "live-opinion"
assert data["corpus"]["taxonomy"]["version"] == "live-taxonomy"
raw = path.read_text()
for header in ("[routing.\"conductor\"]", "[routing.\"verifiers\"]", "[routing.\"second_opinion\"]", "[corpus.\"taxonomy\"]"):
    assert raw.count(header) == 1, header
for name in ("gpt-6-astra", "gpt-oss-120b"):
    assert sum(name in line for line in path.read_text().splitlines() if line.lstrip().startswith("[model")) == 1
PY
cp "$HOME/.grok/config.toml" "$fake_semantic/config.first"
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_semantic/install2.log" 2>&1 || fail "semantic second install failed"
cmp -s "$HOME/.grok/config.toml" "$fake_semantic/config.first" || fail "semantic second install changed config"

# Equivalent duplicate live declarations are diagnosed and safely reduced to one.
export HOME="$fake_duplicate/equivalent-home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[model.gpt-6-astra]
model = "same-pin"

[model."gpt-6-astra"]
model = "same-pin"
EOF
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_duplicate/equivalent.log" 2>&1 || fail "equivalent duplicate install failed"
grep -q 'duplicate equivalent live table' "$fake_duplicate/equivalent.log" || fail "equivalent duplicate was not diagnosed"
python3 - "$HOME/.grok/config.toml" <<'PY' || fail "equivalent duplicate was not deduplicated"
import sys, tomllib
from pathlib import Path
path = Path(sys.argv[1])
assert tomllib.loads(path.read_text())["model"]["gpt-6-astra"]["model"] == "same-pin"
assert sum("gpt-6-astra" in line for line in path.read_text().splitlines() if line.lstrip().startswith("[model")) == 1
PY

# Array-of-tables are independent blocks and survive duplicate-table repair byte-for-byte.
export HOME="$fake_duplicate/array-home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[model.gpt-6-astra]
model = "same-pin"

[model."gpt-6-astra"]
model = "same-pin"

[[events]]
name = "first"
payload = "keep exactly"

[[events]]
name = "second"
payload = "also keep"
EOF
REPO_DIR="$PWD" bash scripts/install.sh >"$fake_duplicate/array.log" 2>&1 || fail "array duplicate repair failed"
python3 - "$HOME/.grok/config.toml" <<'PY' || fail "array-of-tables data was lost or moved"
import sys, tomllib
from pathlib import Path
raw = Path(sys.argv[1]).read_text(encoding="utf-8")
data = tomllib.loads(raw)
assert data["events"] == [
    {"name": "first", "payload": "keep exactly"},
    {"name": "second", "payload": "also keep"},
]
assert raw.index('[[events]]\nname = "first"') < raw.index('[[events]]\nname = "second"')
assert raw.index('payload = "keep exactly"') < raw.index('payload = "also keep"')
PY

# Conflicting duplicate live declarations fail without modifying the file.
export HOME="$fake_duplicate/conflict-home"
mkdir -p "$HOME/.grok"
cat > "$HOME/.grok/config.toml" <<'EOF'
[model.gpt-6-astra]
model = "first-pin"

[model."gpt-6-astra"]
model = "second-pin"
EOF
cp "$HOME/.grok/config.toml" "$fake_duplicate/conflict.before"
if REPO_DIR="$PWD" bash scripts/install.sh >"$fake_duplicate/conflict.log" 2>&1; then
  fail "conflicting duplicate install unexpectedly succeeded"
fi
grep -q 'duplicate table \[model.gpt-6-astra\] has conflicting keys: model' "$fake_duplicate/conflict.log" \
  || { cat "$fake_duplicate/conflict.log" >&2; fail "conflicting duplicate was not diagnosed precisely"; }
cmp -s "$HOME/.grok/config.toml" "$fake_duplicate/conflict.before" || fail "conflicting duplicate was written"

printf 'OK: grok-build install contract\n'
