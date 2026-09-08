#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"

python3 - "$REPO_ROOT" <<'PY'
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
allow_dir = root / "scripts" / "release-check.d"
# The real L1/L2 term lists are private and live in untracked *.local.txt
# siblings of the tracked (format-only) lists; git never sees them, so the
# gate scans its own script and data directory like any other tracked file.
# The one self-reference is the secret allowlist, which quotes the literals
# Gate 4 would otherwise flag in it.
allow_empty_blacklist = os.environ.get("RELEASE_CHECK_ALLOW_EMPTY_BLACKLIST") == "1"

def tracked():
    raw = subprocess.check_output(["git", "ls-files", "-z"])
    return [x for x in raw.decode().split("\0") if x]

def read(path):
    file_path = root / path
    if os.path.islink(file_path):
        return os.readlink(file_path).encode(), True, False
    content = file_path.read_bytes()
    return content, False, b"\0" in content[:8000]

def entries(name):
    result = []
    local = allow_dir / (name[: -len(".txt")] + ".local.txt")
    for path in (allow_dir / name, local):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                result.append(line.strip())
    return result

def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def finding(path, line, category, value):
    return f"{path}:{line}:{category}:{fingerprint(value)}"


def report(gate, failures):
    if failures:
        print(f"FAIL {gate}")
        for item in failures:
            print(f"  {item}")
        return False
    print(f"PASS {gate}")
    return True

files = tracked()
ok = True
terms1 = entries("blacklist-l1.txt")
terms2 = entries("blacklist-l2.txt")
def boundary_pattern(terms):
    if not terms:
        return None
    return re.compile(r"(?i)(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, terms)) + r")(?![A-Za-z0-9_])")

p1 = boundary_pattern(terms1)
p2 = boundary_pattern(terms2)
l1_fail = []
l2_allowed = set(entries("l2-allowlist.txt"))
l2_fail = []
for path in files:
    content, is_symlink, is_binary = read(path)
    if is_binary:
        continue
    text = content.decode("utf-8", errors="replace")
    for match in (p1.finditer(text) if p1 else ()):
        line = text.count("\n", 0, match.start()) + 1
        l1_fail.append(finding(path, line, "l1-blacklist", match.group(0)))
    for match in (p2.finditer(text) if p2 else ()):
        if match.group(0).lower() not in {x.lower() for x in l2_allowed}:
            line = text.count("\n", 0, match.start()) + 1
            l2_fail.append(finding(path, line, "l2-blacklist", match.group(0)))
if not terms1 and not allow_empty_blacklist:
    l1_fail.append("no L1 terms loaded: install scripts/release-check.d/blacklist-l1.local.txt (untracked), or set RELEASE_CHECK_ALLOW_EMPTY_BLACKLIST=1 for a clone without private lists")
if not terms2 and not allow_empty_blacklist:
    l2_fail.append("no L2 terms loaded: install scripts/release-check.d/blacklist-l2.local.txt (untracked), or set RELEASE_CHECK_ALLOW_EMPTY_BLACKLIST=1 for a clone without private lists")
ok &= report("Gate 1 L1 personal-identifier blacklist", l1_fail)
if l2_fail:
    print("FAIL Gate 1 L2 work-tech blacklist")
    print("  Add a deliberate, reviewable term to scripts/release-check.d/l2-allowlist.txt to opt out.")
    for item in l2_fail:
        print(f"  {item}")
    ok = False
else:
    print("PASS Gate 1 L2 work-tech blacklist")

cyr_allow = set(entries("cyrillic-allowlist.txt"))
cyr_fail = []
for path in files:
    content, is_symlink, is_binary = read(path)
    if not is_binary and re.search(r"[\u0400-\u04ff]", content.decode("utf-8", errors="replace")) and path not in cyr_allow:
        cyr_fail.append(path)
ok &= report("Gate 2 Cyrillic allowlist", cyr_fail)

home_re = re.compile(r"/home/([^/\s\"'<>]+)")
home_fail = []
# The gate script quotes the very patterns it scans for; it is exempt from the
# two pattern gates only, and still subject to the term and Cyrillic gates.
pattern_self = {"scripts/release-check.sh"}
for path in files:
    if path in pattern_self:
        continue
    content, is_symlink, is_binary = read(path)
    if is_binary:
        continue
    text = content.decode("utf-8", errors="replace")
    for match in home_re.finditer(text):
        if match.group(1) not in {"user", "someone"}:
            line = text.count("\n", 0, match.start()) + 1
            home_fail.append(finding(path, line, "home", match.group(0)))
ok &= report("Gate 3 /home placeholder allowlist", home_fail)

secret_patterns = [
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{4,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?<![A-Za-z0-9_-])Bearer [A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(?:token|secret|password|passwd)\s*[=:]\s*[\"'][^\"']+[\"']"),
]
secret_allow = {(parts[0], parts[1]) for line in entries("secret-allowlist.txt") if len(parts := line.split(" ", 1)) == 2}
secret_fail = []
for path in files:
    if path in pattern_self or path == "scripts/release-check.d/secret-allowlist.txt":
        continue
    content, is_symlink, is_binary = read(path)
    if is_binary:
        continue
    text = content.decode("utf-8", errors="replace")
    for pattern_index, pattern in enumerate(secret_patterns, 1):
        for match in pattern.finditer(text):
            literal = match.group(0)
            if (path, literal) not in secret_allow:
                line = text.count("\n", 0, match.start()) + 1
                secret_fail.append(finding(path, line, f"secret-pattern-{pattern_index}", literal))
ok &= report("Gate 4 secret-pattern point allowlist", secret_fail)

model_re = re.compile(r"(?:glm-|gemini-|grok-|gpt-|deepseek-|kimi-|minimax-|mimo-|qwen)[0-9](?:[.-][A-Za-z0-9]+)*")
baseline = {}
for line in entries("model-literal-baseline.txt"):
    path, literal, count = line.split()
    baseline[(path, literal)] = int(count)
model_fail = []
for path in files:
    if not (path.startswith("grokbuild/") and path.endswith(".py")):
        continue
    content, is_symlink, is_binary = read(path)
    if is_binary:
        continue
    source = content.decode("utf-8", errors="replace")
    allowed_lines = set()
    if is_symlink:
        # Symlinked Python files are scanned as link-target text, not parsed or dereferenced.
        tree = None
    else:
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if targets and all(isinstance(target, ast.Name) and target.id.endswith("_FALLBACK") for target in targets):
                    if getattr(node, "end_lineno", None):
                        allowed_lines.update(range(node.lineno, node.end_lineno + 1))
        except SyntaxError:
            model_fail.append(f"{path}: cannot parse Python")
            continue
    found = {}
    nonfallback = {}
    for lineno, line in enumerate(source.splitlines(), 1):
        for match in model_re.finditer(line):
            literal = match.group(0)
            found[literal] = found.get(literal, 0) + 1
            if lineno not in allowed_lines:
                nonfallback[literal] = nonfallback.get(literal, 0) + 1
    for literal, count in nonfallback.items():
        expected = baseline.get((path, literal), 0)
        if count > expected:
            model_fail.append(f"{path}: {literal} count {count} exceeds baseline {expected}")
ok &= report("Gate 5 model-literal baseline", model_fail)

# History retains earlier term-list revisions, so some paths are excluded below.
# Publication therefore uses the fresh-init export procedure described in docs/PUBLISHING.md.
history_fail = []
history_pattern = r"(^|[^A-Za-z0-9_])(" + "|".join(map(re.escape, terms1)) + r")([^A-Za-z0-9_]|$)"
for commit in (subprocess.check_output(["git", "rev-list", "--all"], text=True).splitlines() if terms1 else []):
    command = ["git", "grep", "-I", "-i", "-n", "-E", history_pattern, commit, "--", ".", ":(exclude)scripts/release-check.sh", ":(exclude)scripts/release-check.d/*", ":(exclude)tests/test_eval_composition.py"]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        history_fail.append(f"{commit}: git grep error: {result.stderr.strip()}")
    elif result.returncode == 0:
        for entry in result.stdout.splitlines():
            match = re.match(r"[^:]+:(.*?):(\d+):(.*)$", entry)
            if not match:
                history_fail.append(f"{commit}:history-l1:unparseable-match")
                continue
            path, line, content = match.groups()
            term_match = re.search(history_pattern, content)
            value = term_match.group(2) if term_match else content
            history_fail.append(finding(f"{commit}:{path}", line, "history-l1", value))
ok &= report("Gate 6 history L1 blacklist", history_fail)

if not ok:
    print("SUMMARY: FAIL")
    raise SystemExit(1)

commands = [
    ["./tests/grok-route-test.sh"],
    ["bash", "tests/grok-combine-install-test.sh"],
    ["python3", "-m", "pytest", "-q"],
    ["python3", "-m", "grokbuild.render_docs", "check", "--config", "config/config.toml", "--providers", "grokbuild/providers.json", "--intents", "grokbuild/intents.json", "--prose", "grokbuild/contract_prose.json"],
]
for command in commands:
    subprocess.run(command, check=True)
print("PASS Gate 7 test suite finale")
print("SUMMARY: PASS (7 gates)")
PY
