#!/usr/bin/env bash
set -euo pipefail

# Install Grok Build's public configuration, roles, skills, routing modules, and hooks.
# The operation is idempotent and never reads or writes account credentials.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
HOME="${HOME%/}"

ok()   { printf 'OK: %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'SKIP: %s\n' "$*"; }

if [ ! -d "$REPO_DIR/grokbuild" ] || [ ! -d "$REPO_DIR/hooks" ]; then
  printf '%s does not contain grokbuild/ and hooks/\n' "$REPO_DIR" >&2
  exit 1
fi

echo "=== Grok Build: declarative links ==="
link_file() {
  local src="$1" dst="$2"
  if [ ! -e "$src" ]; then
    warn "missing $src"
  elif [ -L "$dst" ]; then
    local cur
    cur="$(readlink "$dst")"
    if [ "$cur" = "$src" ]; then
      ok "$dst"
    else
      ln -sfn "$src" "$dst"
      ok "redirected $dst -> $src"
    fi
  elif [ -e "$dst" ]; then
    warn "$dst exists and is not a symlink; leaving it unchanged"
  else
    mkdir -p "$(dirname "$dst")"
    ln -s "$src" "$dst"
    ok "linked $dst"
  fi
}

chmod +x "$REPO_DIR/hooks/route.py" \
  "$REPO_DIR/grokbuild/cli.py" \
  "$REPO_DIR/grokbuild/conductor.py" \
  "$REPO_DIR/scripts/install.sh" \
  "$REPO_DIR/scripts/grok-route" \
  "$REPO_DIR/tests/grok-route-test.sh" \
  "$REPO_DIR/tests/grok-combine-install-test.sh" 2>/dev/null || true

validate_live_config() {
  GROK_HOME="$HOME/.grok" python3 - "$REPO_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from grokbuild.roles import load_registry, discover_agent_files
    from grokbuild.classify import load_intents
except Exception as exc:  # noqa: BLE001
    print(f"registry unavailable: {exc}")
    sys.exit(2)

reg = load_registry()
intents = load_intents()
issues = [i for i in reg.validate_intents(intents) if i.get("level") == "error"]
role_issues = [i for i in reg.validate_roles() if i.get("level") == "error"]
for issue in issues:
    print(f"{issue.get('intent')}: {issue.get('msg')}" )
for issue in role_issues:
    print(f"{issue.get('role')}: {issue.get('msg')}" )
required = set()
for name in ("security", "implement"):
    intent = intents.get("intents", {}).get(name) or {}
    role = reg.canonical(str(intent.get("role") or name))
    if role:
        required.add(role)
required |= {
    "implement-cheap", "implement-standard", "implement-strong", "implement-hard",
    "implement-ops", "implement-overflow", "implement-cheap-fallback",
    "explore-thorough", "review-hard", "review-independent",
    "security", "security-verify",
}
discoverable = set(discover_agent_files()) | {"general-purpose", "explore", "plan"}
missing = sorted(required - discoverable)
for name in missing:
    print(f"role '{name}' is not a discoverable Grok agent type")
sys.exit(1 if (issues or role_issues or missing) else 0)
PY
}

remove_repo_hook_symlinks() {
  local name dst src target
  for name in route.py route.json; do
    dst="$HOME/.grok/hooks/$name"
    src="$REPO_DIR/hooks/$name"
    if [ -L "$dst" ]; then
      target="$(readlink "$dst")"
      if [ "$target" = "$src" ] || python3 - "$dst" "$target" "$name" "$HOME/.grok" <<'PY'
import os
import sys

link, target, name, grok_home = sys.argv[1:]
if not os.path.isabs(target):
    target = os.path.join(os.path.dirname(link), target)
resolved = os.path.realpath(target)
try:
    outside_grok = os.path.commonpath((resolved, os.path.realpath(grok_home))) != os.path.realpath(grok_home)
except ValueError:
    outside_grok = True
sys.exit(0 if outside_grok and resolved.endswith(os.sep + "hooks" + os.sep + name) else 1)
PY
      then
        rm -f "$dst"
        ok "removed stale repository hook link $dst"
      fi
    fi
  done
}

mkdir -p "$HOME/.grok/agents" "$HOME/.grok/skills" "$HOME/.grok/rules" \
  "$HOME/.grok/hooks" "$HOME/.grok/routing" "$HOME/.grok/routing/fixtures"
link_file "$REPO_DIR/config/config.toml" "$HOME/.grok/config.toml"

# Grok loads model and role declarations from the live user config. When that
# file is account-owned (regular, not a symlink), preserve all live values and
# fill only missing repository declarations. Back up before changes; never
# inspect or print secret values.
if [ -f "$REPO_DIR/config/config.toml" ] && [ -f "$HOME/.grok/config.toml" ] && [ ! -L "$HOME/.grok/config.toml" ]; then
  python3 - "$REPO_DIR/config/config.toml" "$HOME/.grok/config.toml" <<'PY'
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

repo, live = map(Path, sys.argv[1:])
header = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")

# Repository verifier entries and selected routing/corpus tables are merged into
# live config without overwriting account-owned values.
old = live.read_text(encoding="utf-8")
repo_text = repo.read_text(encoding="utf-8")
repo_data = tomllib.loads(repo_text)

# Account-owned managed values are authoritative. Fill only absent tables and
# keys from the repository declaration; never replace an existing live pin.
def fill_missing_tables(live_text: str, repo_text: str) -> str:
    live_lines = live_text.splitlines(keepends=True)
    repo_lines = repo_text.splitlines(keepends=True)
    headers = {m.group(1).strip() for line in live_lines if (m := header.match(line))}
    additions = []
    index = 0
    while index < len(repo_lines):
        match = header.match(repo_lines[index])
        if not match:
            index += 1
            continue
        section = match.group(1).strip()
        start = index
        index += 1
        while index < len(repo_lines) and not header.match(repo_lines[index]):
            index += 1
        block = repo_lines[start:index]
        if section not in headers:
            additions.extend(block)
            continue
        # Existing tables are left byte-for-byte intact: their live values,
        # including multiline values, are authoritative.
    result = "".join(live_lines).rstrip("\n")
    if additions:
        result += "\n\n" + "".join(additions).rstrip("\n")
    return result + "\n"

new = fill_missing_tables(old, repo_text)

def relocate_tables(text: str, sections: tuple[str, ...]) -> str:
    lines = text.splitlines(keepends=True)
    kept = []
    blocks = []
    index = 0
    while index < len(lines):
        match = header.match(lines[index])
        if match and match.group(1).strip() in sections:
            start = index
            index += 1
            while index < len(lines) and not header.match(lines[index]):
                index += 1
            blocks.append(lines[start:index])
        else:
            kept.append(lines[index])
            index += 1
    result = "".join(kept).rstrip("\n")
    for block in blocks:
        result += "\n\n" + "".join(block).rstrip("\n")
    return result + "\n"

new = relocate_tables(new, ("routing.conductor", "routing.second_opinion", "corpus.taxonomy"))
verifier_key = "$REPO_ROOT"
verifier_commands = ["./tests/grok-route-test.sh", "./tests/grok-combine-install-test.sh"]
verifier_line = f'"{verifier_key}" = ["{verifier_commands[0]}", "{verifier_commands[1]}"]\n'
try:
    parsed = tomllib.loads(new)
except tomllib.TOMLDecodeError as exc:
    raise SystemExit(f"refusing to write invalid merged config: {exc}") from exc
routing = parsed.get("routing", {})
initial_tables = {
    "routing.conductor": routing.get("conductor", {}) if isinstance(routing, dict) else {},
    "routing.second_opinion": routing.get("second_opinion", {}) if isinstance(routing, dict) else {},
    "corpus.taxonomy": parsed.get("corpus", {}).get("taxonomy", {}) if isinstance(parsed.get("corpus", {}), dict) else {},
}
verifiers = routing.get("verifiers", {}) if isinstance(routing, dict) else {}
if not isinstance(verifiers, dict):
    raise SystemExit("refusing to write config with malformed [routing.verifiers]")
if not verifiers and "routing" not in parsed:
    new = new.rstrip("\n") + "\n\n[routing.verifiers]\n" + verifier_line
elif "verifiers" not in routing:
    new = new.rstrip("\n") + "\n\n[routing.verifiers]\n" + verifier_line
elif verifier_key not in verifiers:
    lines = new.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = header.match(line)
        if match and match.group(1).strip() == "routing.verifiers":
            lines.insert(index + 1, verifier_line)
            new = "".join(lines)
            break
    else:
        raise SystemExit("could not locate [routing.verifiers] header")

def ensure_table(text: str, section: str, repo_table: dict) -> str:
    lines = text.splitlines(keepends=True)
    section_index = next(
        (index for index, line in enumerate(lines)
         if (match := header.match(line)) and match.group(1).strip() == section),
        None,
    )
    rendered = [f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in repo_table.items()]
    if section_index is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + f"\n[{section}]\n" + "".join(rendered)
    present = set()
    for line in lines[section_index + 1:]:
        match = header.match(line)
        if match:
            break
        assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if assignment:
            present.add(assignment.group(1))
    missing = [line for key, line in zip(repo_table, rendered) if key not in present]
    if missing:
        lines[section_index + 1:section_index + 1] = missing
    return "".join(lines)

import json
for section in ("routing.conductor", "routing.second_opinion", "corpus.taxonomy"):
    parent, child = section.split(".")
    table = repo_data.get(parent, {}).get(child, {})
    if not isinstance(table, dict):
        raise SystemExit(f"refusing to merge malformed [{section}]")
    new = ensure_table(new, section, table)
try:
    final = tomllib.loads(new)
except tomllib.TOMLDecodeError as exc:
    raise SystemExit(f"refusing to write invalid final config: {exc}") from exc
if final.get("routing", {}).get("verifiers", {}).get(verifier_key) != verifier_commands:
    raise SystemExit("[routing.verifiers].$REPO_ROOT must contain the Grok routing test commands")
for section in ("routing.conductor", "routing.second_opinion", "corpus.taxonomy"):
    parent, child = section.split(".")
    ensured = final.get(parent, {}).get(child, {})
    repo_table = repo_data[parent][child]
    initial = initial_tables[section]
    if any(key not in ensured for key in repo_table):
        raise SystemExit(f"[{section}] must contain every repository key")
    if any(key not in initial and ensured[key] != value for key, value in repo_table.items()):
        raise SystemExit(f"[{section}] must contain repository values for missing keys")
if new != old:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = live.with_name(f"config.toml.before-combine-{stamp}.bak")
    counter = 1
    while backup.exists():
        backup = live.with_name(f"config.toml.before-combine-{stamp}-{counter}.bak")
        counter += 1
    shutil.copy2(live, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{live.name}.", dir=live.parent)
    try:
        os.fchmod(fd, live.stat().st_mode & 0o7777)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, live)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
PY
  ok "$HOME/.grok/config.toml merged with live pins authoritative (backup created when changed)"
  info "merge summary: existing live model, role, and routing values preserved; missing repository declarations filled"
fi
link_file "$REPO_DIR/rules/security-models.md" "$HOME/.grok/rules/security-models.md"
for agent in \
  implement review security \
  implement-cheap implement-cheap-fallback implement-standard implement-strong implement-hard \
  implement-ops implement-overflow explore-thorough explore-risk \
  review-hard review-independent security-verify plan-hard \
  consilium-analyst consilium-challenger consilium-arbiter \
  researcher researcher-analyst researcher-challenger \
  planner-a planner-b planner-c planner-strong plan-comparator \
  criterion-judge judge-primary judge-independent judge-disagreement \
  judge-frontier-code judge-frontier-general judge-challenger-agentic judge-challenger-structural verifier-planner \
  frontier-resolver frontier-resolver-standby \
  visual-intake visual-intake-deep; do
  link_file "$REPO_DIR/agents/${agent}.md" "$HOME/.grok/agents/${agent}.md"
done
for skill in implement model-routing rebuild-stack security corpus-sync bounty-triage; do
  mkdir -p "$HOME/.grok/skills/$skill"
  link_file "$REPO_DIR/skills/${skill}/SKILL.md" "$HOME/.grok/skills/${skill}/SKILL.md"
done
for routing in \
  classify.py intents.json roles_default.json station_fallback.json profiles.json providers.json barrier_lenses.json README.md \
  decision.py redact.py _repo.py persist.py endpoint_resolution.py evidence.py remediation.py safe_alternatives.py transactions.py roles.py tiers.py features.py policy.py state.py cache.py payloads.py visual_cache.py visual_intake.py verifiers.py availability.py compose.py settlement.py pipeline.py router.py stats.py cli.py conductor.py discovery.py \
  task_verify.py task_evidence.py artifact_judge.py verifier_planner.py selector.py frontier.py datasets.py \
  corpus_sync.py; do
  link_file "$REPO_DIR/grokbuild/$routing" "$HOME/.grok/routing/$routing"
done
link_file "$REPO_DIR/grokbuild/fixtures/workloads.json" "$HOME/.grok/routing/fixtures/workloads.json"

write_roles_manifest() {
  local manifest="$HOME/.grok/grok-roles-manifest.json"
  python3 - "$REPO_DIR" "$manifest" <<'PY'
import json
import sys
import time
from pathlib import Path

repo = Path(sys.argv[1])
manifest = Path(sys.argv[2])
roles = [
    "implement", "review", "security",
    "implement-cheap", "implement-cheap-fallback", "implement-standard", "implement-strong",
    "implement-hard", "implement-ops", "implement-overflow", "explore-thorough", "explore-risk",
    "review-hard", "review-independent", "security-verify", "plan-hard",
    "consilium-analyst", "consilium-challenger", "consilium-arbiter",
    "researcher", "researcher-analyst", "researcher-challenger",
    "planner-a", "planner-b", "planner-c", "planner-strong", "plan-comparator",
    "criterion-judge", "judge-primary", "judge-independent", "judge-disagreement",
    "judge-frontier-code", "judge-frontier-general", "judge-challenger-agentic", "judge-challenger-structural", "verifier-planner",
    "frontier-resolver", "frontier-resolver-standby",
    "visual-intake", "visual-intake-deep",
]
data = {
    "version": 1,
    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "repo": str(repo),
    "roles": [
        {"name": name, "source": str(repo / "agents" / f"{name}.md")}
        for name in roles
    ],
}
manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
}
write_roles_manifest

install_hooks=1
if ! validate_live_config; then
  install_hooks=0
  warn "live ~/.grok/config.toml does not expose the required route-hook roles; hooks were not installed"
  remove_repo_hook_symlinks
fi
if [ "$install_hooks" -eq 1 ]; then
  link_file "$REPO_DIR/hooks/route.py" "$HOME/.grok/hooks/route.py"
  link_file "$REPO_DIR/hooks/route.json" "$HOME/.grok/hooks/route.json"
else
  warn "route.py and route.json were skipped"
fi

if [ ! -L "$HOME/.grok/config.toml" ]; then
  ok "$HOME/.grok/config.toml is an account-owned regular file; managed tables were merged"
else
  ok "$HOME/.grok/config.toml -> $(readlink "$HOME/.grok/config.toml")"
fi

# Report subscription coverage using provider and role data, never credential values.
python3 - "$REPO_DIR" <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from grokbuild.tiers import (
    TIER_PROVIDER_SUBSETS,
    achieved_level,
    load_provider_data,
    load_role_data,
    provider_credentials,
    resolved_roles,
    role_provider_map,
    roles_for_providers,
)

provider_data = load_provider_data()
role_data = load_role_data()
role_map = role_provider_map(provider_data, role_data)
credentials = provider_credentials(os.environ, os.environ.get("HOME"))
resolved = resolved_roles(credentials, role_map)
print("=== Grok Build: subscription tier report ===")
for provider, spec in provider_data["providers"].items():
    credential = spec.get("credential_env") or spec.get("credential_path") or "none"
    present = credentials.get(provider, False)
    print(f"provider {provider}: class {spec.get('subscription_class', 'unknown')}; credential {credential}: {'present' if present else 'absent'}")

for role, provider in sorted(role_map.items()):
    signal = bool(provider_data["providers"][provider].get("require_availability_signal"))
    suffix = "; availability signal required" if signal else ""
    print(f"role {role}: {'resolvable' if role in resolved else 'not resolvable'} ({provider}){suffix}")

level, _ = achieved_level(credentials, role_map)
print(f"achieved level: {level}")
for name in ("Minimum", "Recommended", "Full"):
    providers = TIER_PROVIDER_SUBSETS[name]
    missing_providers = sorted(providers - {provider for provider, present in credentials.items() if present})
    required = roles_for_providers(providers, role_map)
    missing_roles = sorted(required - resolved)
    if missing_providers or missing_roles:
        if missing_providers:
            print(f"{name} missing provider credentials: {', '.join(missing_providers)}")
        if missing_roles:
            print(f"{name} missing roles: {', '.join(missing_roles)}")
        break
else:
    print("all configured provider credentials are present; Full still requires the xai availability signal")
PY
