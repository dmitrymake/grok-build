#!/usr/bin/env python3
"""Hidden acceptance test for T3. Controller-owned; never placed in a workspace.

Usage: python3 verify_t3.py <workspace_dir>
Builds a throwaway root with an outward symlink and a neighbour dir, then
checks both the exploit vectors are closed and the legitimate read still works.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path


def load(ws):
    spec = importlib.util.spec_from_file_location("fileserve", Path(ws) / "fileserve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.read_served_file


def main(ws):
    fn = load(ws)
    fails = []

    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        root = base / "root"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "readme.txt").write_bytes(b"OK-LEGIT")
        secret = base / "secret.txt"
        secret.write_bytes(b"TOP-SECRET")
        # Neighbour dir sharing the root prefix.
        neighbour = base / "root_evil"
        neighbour.mkdir()
        (neighbour / "loot.txt").write_bytes(b"NEIGHBOUR-LOOT")
        # Symlink inside root pointing outward.
        try:
            os.symlink(secret, root / "link_out")
            have_symlink = True
        except OSError:
            have_symlink = False

        r = str(root)

        def legit():
            try:
                return fn(r, "docs/readme.txt") == b"OK-LEGIT"
            except Exception as exc:
                fails.append(f"legit nested read broke ({type(exc).__name__})")
                return False

        def blocked(user_path, label):
            try:
                data = fn(r, user_path)
            except PermissionError:
                return
            except FileNotFoundError:
                # Acceptable only if the path truly does not resolve to a real
                # outside file; for these vectors it does, so treat as a miss.
                fails.append(f"{label} (FileNotFoundError, expected PermissionError)")
                return
            except Exception as exc:
                fails.append(f"{label} (raised {type(exc).__name__})")
                return
            fails.append(f"{label} (LEAKED {data[:16]!r})")

        if not legit():
            pass
        blocked("../secret.txt", "dotdot traversal blocked")
        blocked("docs/../../secret.txt", "nested dotdot traversal blocked")
        blocked("/etc/passwd", "absolute path blocked")
        blocked("../root_evil/loot.txt", "neighbour prefix trick blocked")
        if have_symlink:
            blocked("link_out", "outward symlink blocked")

        # Missing-but-internal path should be FileNotFoundError, not Permission.
        try:
            fn(r, "docs/nope.txt")
            fails.append("missing internal file should raise")
        except FileNotFoundError:
            pass
        except PermissionError:
            fails.append("missing internal file wrongly treated as attack")
        except Exception as exc:
            fails.append(f"missing internal file raised {type(exc).__name__}")

    if fails:
        print(f"T3 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T3 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
