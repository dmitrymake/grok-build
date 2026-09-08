#!/usr/bin/env python3
"""Hidden acceptance test for T5 (crash-safe journal). Controller-owned.

Usage: python3 verify_t5.py <workspace_dir>
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

fails = []


def ok(cond, label):
    if not cond:
        fails.append(label)


def step(label, fn):
    try:
        return fn()
    except Exception as exc:
        fails.append(f"{label}: raised {type(exc).__name__}: {exc}")
    return None


def load(ws):
    spec = importlib.util.spec_from_file_location("journal", Path(ws) / "journal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(ws):
    mod = load(ws)

    with tempfile.TemporaryDirectory() as raw:
        d = os.path.join(raw, "j1")
        os.makedirs(d)
        ok(mod.read_state(d) == {}, "empty dir reads as empty state")
        ok(mod.append_event(d, "set", "a", "1") == 1, "first append is seq 1")
        ok(mod.append_event(d, "set", "b", "x\ny") == 2, "value with newline accepted")
        ok(mod.append_event(d, "del", "missing") == 3, "del of absent key is valid")
        ok(mod.append_event(d, "set", "", "empty-key") == 4, "empty key valid")
        st = mod.read_state(d)
        ok(st == {"a": "1", "b": "x\ny", "": "empty-key"}, f"folded state wrong: {st}")

    # Torn tail: ignored by read, trimmed by next append, seq continues.
    with tempfile.TemporaryDirectory() as raw:
        d = raw
        p = os.path.join(d, "journal.log")
        with open(p, "w") as h:
            h.write(json.dumps({"seq": 1, "op": "set", "key": "k", "value": "v"}) + "\n")
            h.write('{"seq": 2, "op": "set", "key": "torn", "val')  # killed mid-write
        ok(step("torn-tail read", lambda: mod.read_state(d)) == {"k": "v"}, "torn tail ignored on read")
        ok(step("append over torn tail", lambda: mod.append_event(d, "set", "n", "2")) == 2, "append trims torn tail and reuses seq 2")
        ok(step("read after trim", lambda: mod.read_state(d)) == {"k": "v", "n": "2"}, "state after trim+append")

    # Journal that is ONLY a torn line.
    with tempfile.TemporaryDirectory() as raw:
        p = os.path.join(raw, "journal.log")
        with open(p, "w") as h:
            h.write('{"seq": 1, "op"')
        ok(step("only-torn read", lambda: mod.read_state(raw)) == {}, "only-torn journal reads empty")
        ok(step("append over only-torn", lambda: mod.append_event(raw, "set", "a", "1")) == 1, "append over only-torn starts at 1")

    # Duplicate seq applies once, first occurrence wins.
    with tempfile.TemporaryDirectory() as raw:
        p = os.path.join(raw, "journal.log")
        with open(p, "w") as h:
            h.write(json.dumps({"seq": 1, "op": "set", "key": "k", "value": "first"}) + "\n")
            h.write(json.dumps({"seq": 1, "op": "set", "key": "k", "value": "second"}) + "\n")
            h.write(json.dumps({"seq": 2, "op": "set", "key": "z", "value": "3"}) + "\n")
        ok((step("dup-seq read", lambda: mod.read_state(raw)) or {}).get("k") == "first", "duplicate seq: first occurrence wins")

    # Gap in whole lines is corruption.
    with tempfile.TemporaryDirectory() as raw:
        p = os.path.join(raw, "journal.log")
        with open(p, "w") as h:
            h.write(json.dumps({"seq": 1, "op": "set", "key": "a", "value": "1"}) + "\n")
            h.write(json.dumps({"seq": 3, "op": "set", "key": "b", "value": "2"}) + "\n")
        try:
            mod.read_state(raw)
            fails.append("seq gap not detected")
        except RuntimeError:
            pass
        except Exception as exc:
            fails.append(f"seq gap raised {type(exc).__name__}, want RuntimeError")

    # Durability: append must fsync something.
    with tempfile.TemporaryDirectory() as raw:
        counts = {"fsync": 0}
        real_fsync = os.fsync
        mod.os.fsync = lambda fd: (counts.__setitem__("fsync", counts["fsync"] + 1), real_fsync(fd))[1]
        try:
            mod.append_event(raw, "set", "a", "1")
        finally:
            mod.os.fsync = real_fsync
        ok(counts["fsync"] >= 1, "append never calls os.fsync - not crash-durable")

    # Compact: renumber from 1, sorted keys, next append continues; recompact no-op.
    with tempfile.TemporaryDirectory() as raw:
        for i, (op, k, v) in enumerate(
            [("set", "b", "1"), ("set", "a", "2"), ("del", "b", None),
             ("set", "c", "3"), ("set", "a", "4")]):
            mod.append_event(raw, op, k, v)
        mod.compact(raw)
        lines = [json.loads(l) for l in open(os.path.join(raw, "journal.log")) if l.strip()]
        ok([l["seq"] for l in lines] == list(range(1, len(lines) + 1)), "compact renumbers from 1")
        ok([l["key"] for l in lines] == sorted(l["key"] for l in lines), "compact sorts keys")
        ok(mod.read_state(raw) == {"a": "4", "c": "3"}, "state preserved by compact")
        nxt = mod.append_event(raw, "set", "d", "5")
        ok(nxt == len(lines) + 1, f"append after compact continues seq ({nxt})")
        st_before = mod.read_state(raw)
        mod.compact(raw)
        mod.compact(raw)
        ok(mod.read_state(raw) == st_before, "recompacting is a lossless no-op")

    # Crash-atomicity of compact: kill at the replace point leaves old journal intact.
    with tempfile.TemporaryDirectory() as raw:
        mod.append_event(raw, "set", "a", "1")
        mod.append_event(raw, "set", "b", "2")
        before = mod.read_state(raw)

        class Killed(Exception):
            pass

        real_replace = os.replace
        def killing_replace(src, dst):
            raise Killed()
        mod.os.replace = killing_replace
        try:
            try:
                mod.compact(raw)
                fails.append("compact never uses an atomic replace (rewrote in place?)")
            except Killed:
                pass
            except Exception as exc:
                fails.append(f"compact crash-sim raised {type(exc).__name__} (in-place write?)")
        finally:
            mod.os.replace = real_replace
        after = mod.read_state(raw)
        ok(after == before, f"crash during compact lost data: {after} != {before}")

    if fails:
        print(f"T5 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T5 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
