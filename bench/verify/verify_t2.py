#!/usr/bin/env python3
"""Hidden acceptance test for T2. Controller-owned; never placed in a workspace.

Usage: python3 verify_t2.py <workspace_dir>
"""
import importlib.util
import sys
from pathlib import Path


def load(ws):
    spec = importlib.util.spec_from_file_location("retention", Path(ws) / "retention.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.snapshots_to_delete


def snaps(*days):
    return [{"name": f"s{d}", "epoch_day": d} for d in days]


def main(ws):
    fn = load(ws)
    fails = []

    def kept(snapshots, kd, kw):
        deleted = set(fn([dict(s) for s in snapshots], kd, kw))
        return {s["name"] for s in snapshots} - deleted, deleted

    def ok(cond, label):
        if not cond:
            fails.append(label)

    # Empty input.
    ok(fn([], 3, 2) == [], "empty input deletes nothing")

    # Safety invariant: newest never deleted, even at zero keeps.
    keep, dele = kept(snaps(1, 8, 15, 22, 30), 0, 0)
    ok("s30" in keep, "newest survives keep_daily=0 keep_weekly=0")
    ok(dele == {"s1", "s8", "s15", "s22"}, "zero keeps delete all but the protected newest")

    # Daily keeps N most recent distinct.
    keep, _ = kept(snaps(1, 2, 3, 4, 5), 2, 0)
    ok(keep == {"s4", "s5"}, "keep_daily=2 keeps two newest")

    # Weekly buckets: one per bucket, newest of bucket, for keep_weekly newest buckets.
    # days 1,2 -> bucket0; 8 -> bucket1; 15 -> bucket2; 22 -> bucket3
    keep, _ = kept(snaps(1, 2, 8, 15, 22), 0, 2)
    ok(keep == {"s15", "s22"}, "keep_weekly=2 keeps newest of two newest buckets (plus newest safety)")

    # Daily and weekly do not cannibalize each other: days 1,8,9,10 put 9 and 10
    # in the same weekly bucket, so daily keeps s10+s9 while weekly keeps s10+s1.
    keep, _ = kept(snaps(1, 8, 9, 10), 2, 2)
    ok("s10" in keep and "s9" in keep and "s1" in keep, "daily and weekly slots are independent")
    ok(keep == {"s1", "s9", "s10"}, "overlap counted once, only s8 is deleted")

    # Multiple snapshots same day: distinct daily slots are distinct days.
    two_same = [
        {"name": "a", "epoch_day": 5},
        {"name": "b", "epoch_day": 5},
        {"name": "c", "epoch_day": 6},
    ]
    deleted = set(fn(two_same, 2, 0))
    ok("c" in ({"a", "b", "c"} - deleted), "newest day kept")
    ok(("a" in ({"a", "b", "c"} - deleted)) or ("b" in ({"a", "b", "c"} - deleted)),
       "two daily slots span two days, not one")

    if fails:
        print(f"T2 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T2 PASS (9/9)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
