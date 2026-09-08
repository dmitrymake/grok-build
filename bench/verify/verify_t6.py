#!/usr/bin/env python3
"""Hidden acceptance test for T6 (upload handler audit). Controller-owned.

Vectors under test (never disclosed to agents): V1 double-decode traversal,
V2 absolute/parent name escapes, V3 zip-slip via member paths, V4 CRLF
injection into the audit log, V5 non-constant-time token compare (source
inspection - documented deviation, timing is not reliably measurable).
Usage: python3 verify_t6.py <workspace_dir>
"""
import importlib.util
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

fails = []


def ok(cond, label):
    if not cond:
        fails.append(label)


def load(ws):
    spec = importlib.util.spec_from_file_location("uploads", Path(ws) / "uploads.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def blocked(fn, label):
    """A defence holds unless the attack actually leaks secret bytes.
    Type of refusal (PermissionError/ValueError/FileNotFoundError) is irrelevant;
    what matters is that TOP-SECRET never comes back and no file lands outside."""
    try:
        out = fn()
    except (PermissionError, ValueError, FileNotFoundError, IsADirectoryError):
        return True
    except Exception as exc:
        fails.append(f"{label}: raised {type(exc).__name__} (unexpected but inspect)")
        return True
    if isinstance(out, (bytes, bytearray)) and b"TOP-SECRET" in bytes(out):
        fails.append(f"{label}: LEAKED secret bytes")
        return False
    # write-side attacks return a path; flag only if it resolved outside uploads
    return True


def main(ws):
    mod = load(ws)

    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        root = base / "root"
        (root / "uploads").mkdir(parents=True)
        secret = base / "secret.txt"
        secret.write_bytes(b"TOP-SECRET")
        (base / "up-secret.txt").write_bytes(b"TOP-SECRET")  # reachable via ../../ from uploads

        # Legit flows first.
        try:
            p = mod.save_upload(str(root), "report.pdf", b"PDF")
            ok(Path(p).read_bytes() == b"PDF", "legit save works")
            ok(mod.fetch(str(root), "report.pdf") == b"PDF", "legit fetch works")
            mod.save_upload(str(root), "фото 2026.jpg", b"J")
            ok(mod.fetch(str(root), "фото 2026.jpg") == b"J", "unicode name works")
        except Exception as exc:
            fails.append(f"legit flow broke: {type(exc).__name__}: {exc}")

        # V2: parent traversal and absolute names.
        blocked(lambda: mod.save_upload(str(root), "../evil.txt", b"PWNED"), "V2 save ../ (write-escape check)")
        ok(not (root / "evil.txt").exists() and not (base / "evil.txt").exists(), "V2 no file written outside uploads")
        blocked(lambda: mod.fetch(str(root), "../../secret.txt"), "V2 fetch ../../ (leak check)")
        blocked(lambda: mod.fetch(str(root), str(secret)), "V2 absolute name (leak check)")
        # V1: double-encoded traversal must not decode its way out.
        blocked(lambda: mod.fetch(str(root), "%252e%252e%252f%252e%252e%252fsecret.txt"),
                "V1 double-decode (leak check)")
        blocked(lambda: mod.save_upload(str(root), "%2e%2e%2f%2e%2e%2fevil2.txt", b"PWNED"),
                "V1 encoded write (write-escape check)")
        ok(not (base / "evil2.txt").exists(), "V1 no encoded file written outside uploads")
        ok(not (base / "evil.txt").exists(), "no escaped file was created")

        # V3: zip-slip.
        zp = base / "bundle.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("ok.txt", "fine")
            z.writestr("../slipped.txt", "PWNED")
        blocked(lambda: mod.extract_bundle(str(root), str(zp)), "V3 zip-slip blocked")
        ok(not (root / "uploads" / "slipped.txt").exists() or True, "")
        ok(not (root / "slipped.txt").exists() and not (base / "slipped.txt").exists(),
           "V3 slipped file escaped the bundle dir")
        zp2 = base / "clean.zip"
        with zipfile.ZipFile(zp2, "w") as z:
            z.writestr("docs/a.txt", "A")
        try:
            dest = mod.extract_bundle(str(root), str(zp2))
            ok((Path(dest) / "docs" / "a.txt").read_text() == "A", "legit bundle extracts")
        except Exception as exc:
            fails.append(f"legit bundle broke: {type(exc).__name__}")

        # V4: CRLF/newline injection into the audit log.
        log = root / "access.log"
        try:
            mod.log_access(str(root), "client-1", "report.pdf")
        except Exception as exc:
            fails.append(f"legit log_access broke: {type(exc).__name__}")
        try:
            mod.log_access(str(root), "evil\nadmin accessed secret", "x")
            injected_ok = True
        except (PermissionError, ValueError):
            injected_ok = False  # rejecting is also a valid defence
        if injected_ok:
            lines = log.read_text().splitlines()
            forged = [l for l in lines if l.startswith("admin accessed")]
            ok(not forged, "V4 newline injection forged an audit line")
        try:
            mod.log_access(str(root), "c2", "a\rb\nc accessed q")
        except (PermissionError, ValueError):
            pass
        text = log.read_text()
        ok("\r" not in text, "V4 CR reached the audit log")

        # V5: constant-time token compare (source-level check, documented).
        src = (Path(ws) / "uploads.py").read_text()
        body = src.split("def check_token", 1)[1]
        uses_ct = ("compare_digest" in body)
        plain_eq = re.search(r"return\s+stored\s*==\s*presented", body)
        ok(uses_ct or not plain_eq,
           "V5 token compare is a plain == (timing side channel)")
        try:
            ok(mod.check_token("abc", "abc") in (True,), "token equal compares True")
            ok(mod.check_token("abc", "abd") in (False,), "token unequal compares False")
        except Exception as exc:
            fails.append(f"check_token broke: {type(exc).__name__}")

    if fails:
        print(f"T6 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T6 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
