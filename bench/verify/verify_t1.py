#!/usr/bin/env python3
"""Hidden acceptance test for T1. Controller-owned; never placed in a workspace.

Usage: python3 verify_t1.py <workspace_dir>
Exit 0 = verified pass; exit 1 = fail (prints the failing checks).
"""
import importlib.util
import sys
from pathlib import Path


def load(ws):
    spec = importlib.util.spec_from_file_location("pagerange", Path(ws) / "pagerange.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.parse_page_range


def main(ws):
    fn = load(ws)
    fails = []

    def ok(produce, expected, label):
        try:
            got = produce()
        except Exception as exc:
            fails.append(f"{label} (raised {type(exc).__name__})")
            return
        if got != expected:
            fails.append(f"{label} (got {got!r})")

    def raises(spec, label):
        try:
            fn(spec)
        except ValueError:
            return
        except Exception as exc:  # wrong exception type still counts as a miss
            fails.append(f"{label} (raised {type(exc).__name__}, not ValueError)")
            return
        fails.append(f"{label} (no error)")

    ok(lambda: fn("1-3,5,7-9"), [1, 2, 3, 5, 7, 8, 9], "basic expansion inclusive both ends")
    ok(lambda: fn("5"), [5], "single page")
    ok(lambda: fn("5,1-3,2-4"), [1, 2, 3, 4, 5], "overlap dedup and sort")
    ok(lambda: fn("3-3"), [3], "degenerate range is one page")
    ok(lambda: fn(" 1 - 3 , 5 "), [1, 2, 3, 5], "whitespace normalized")
    ok(lambda: fn(""), [], "empty string is empty list")
    ok(lambda: fn("   "), [], "blank string is empty list")
    ok(lambda: fn("2,2,2"), [2], "repeated singletons dedup")

    raises("5-3", "reversed range rejected")
    raises("0", "zero rejected")
    raises("-2", "negative rejected")
    raises("1,,3", "empty element rejected")
    raises("1-", "dangling range rejected")
    raises("abc", "non-numeric rejected")
    raises("1-3,x", "mixed garbage rejected")

    if fails:
        print(f"T1 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T1 PASS (15/15)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
