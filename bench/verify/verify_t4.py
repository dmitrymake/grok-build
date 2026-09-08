#!/usr/bin/env python3
"""Hidden acceptance test for T4 (token bucket). Controller-owned.

Usage: python3 verify_t4.py <workspace_dir>
"""
import importlib.util
import sys
import threading
import time as real_time

REAL_MONO = real_time.monotonic  # captured before the bomb is installed
REAL_SLEEP = real_time.sleep
from pathlib import Path

fails = []


def ok(cond, label):
    if not cond:
        fails.append(label)


def step(label, fn):
    try:
        return fn()
    except AssertionError as exc:
        fails.append(f"{label}: {exc}")
    except Exception as exc:
        fails.append(f"{label}: raised {type(exc).__name__}: {exc}")
    return None


def with_deadline(label, fn, seconds):
    """Run fn in a thread; a hang means the implementation ignores its timeout."""
    box = {}
    def runner():
        try:
            box["r"] = fn()
        except Exception as exc:
            box["e"] = exc
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        fails.append(f"{label}: call hung past {seconds}s (timeout contract ignored)")
        return None
    if "e" in box:
        fails.append(f"{label}: raised {type(box['e']).__name__}: {box['e']}")
        return None
    return box.get("r")


def load(ws):
    spec = importlib.util.spec_from_file_location("ratelimit", Path(ws) / "ratelimit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def main(ws):
    mod = load(ws)
    TB = mod.TokenBucket

    # Direct-time bomb: the implementation must only read time via the clock.
    def bomb():
        raise AssertionError("implementation called time.monotonic()/time.time() directly")

    saved = (mod.time.monotonic, mod.time.time)
    mod.time.monotonic = bomb
    mod.time.time = bomb
    try:
      try:
        clk = FakeClock()
        b = TB(10.0, 5.0, clock=clk)
        ok(abs(b.available() - 10.0) < 1e-9, "bucket starts full")
        ok(b.try_acquire(4.0) is True, "acquire within balance")
        ok(abs(b.available() - 6.0) < 1e-9, "balance after acquire")
        ok(b.try_acquire(7.0) is False, "insufficient -> False")
        ok(abs(b.available() - 6.0) < 1e-9, "failed acquire must not change state")
        clk.value += 0.4  # 0.4s * 5/s = +2.0
        ok(abs(b.available() - 8.0) < 1e-9, "fractional continuous refill")
        before = b.available()
        _ = b.available()
        ok(abs(b.available() - before) < 1e-9, "same clock reading adds nothing")
        clk.value += 100.0
        ok(abs(b.available() - 10.0) < 1e-9, "refill caps at capacity")
        ok(b.try_acquire(11.0) is False, "n above capacity is always False, not an error")

        for bad in (lambda: TB(0, 1), lambda: TB(1, 0), lambda: TB(-1, 1)):
            try:
                bad(); fails.append("bad constructor accepted")
            except ValueError:
                pass
        try:
            b.try_acquire(0); fails.append("n=0 accepted")
        except ValueError:
            pass
        try:
            b.acquire(1, timeout=-1); fails.append("negative timeout accepted")
        except ValueError:
            pass

        # Race: exactly one of two threads may win the last token.
        for trial in range(50):
            clk2 = FakeClock()
            b2 = TB(1.0, 0.000001, clock=clk2)
            results = []
            barrier = threading.Barrier(2)

            def worker():
                barrier.wait()
                results.append(b2.try_acquire(1.0))

            ts = [threading.Thread(target=worker) for _ in range(2)]
            for t in ts: t.start()
            for t in ts: t.join()
            if sum(results) != 1:
                fails.append(f"double-spend race: {results} on trial {trial}")
                break

        # Concurrent stress: total granted never exceeds start + refilled.
        clk3 = FakeClock()
        b3 = TB(50.0, 100.0, clock=clk3)
        granted = []
        lock = threading.Lock()
        stop = threading.Event()

        def hammer():
            while not stop.is_set():
                if b3.try_acquire(3.0):
                    with lock:
                        granted.append(3.0)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads: t.start()
        for _ in range(40):
            REAL_SLEEP(0.002)
            clk3.value += 0.05  # +5 tokens per step
        stop.set()
        for t in threads: t.join()
        refilled = 40 * 0.05 * 100.0
        ok(sum(granted) <= 50.0 + refilled + 1e-6,
           f"over-grant under contention: granted {sum(granted)} > {50.0 + refilled}")
        ok(b3.available() >= -1e-9, "tokens went negative under contention")

        # Blocking acquire honours the injected clock for its timeout.
        class TickClock:
            def __init__(self):
                self.t0 = REAL_MONO()
            def __call__(self):
                return (REAL_MONO() - self.t0) * 4.0  # 4x speed

        tc = TickClock()
        b4 = TB(1.0, 1.0, clock=tc)
        ok(with_deadline("acquire available", lambda: b4.acquire(1.0, timeout=5.0), 5) is True,
           "blocking acquire grabs available token")
        start = REAL_MONO()
        got = with_deadline("acquire refill-wait", lambda: b4.acquire(0.8, timeout=2.0), 5)
        span = REAL_MONO() - start
        ok(got is True, "blocking acquire waits for refill and succeeds")
        ok(span < 1.5, f"acquire slept in real time ignoring the injected clock ({span:.2f}s)")
        b5 = TB(1.0, 0.1, clock=tc)
        b5.try_acquire(1.0)
        start = REAL_MONO()
        got = with_deadline("acquire timeout path", lambda: b5.acquire(1.0, timeout=0.4), 5)
        span = REAL_MONO() - start
        ok(got is False, "timeout expires without a grant")
        ok(span < 1.0, f"timeout measured in injected clock seconds ({span:.2f}s real)")
        ok(abs(b5.available()) < 0.2, "timed-out acquire must not deduct")
      except Exception as exc:
        fails.append(f"contract body crashed: {type(exc).__name__}: {exc}")
    finally:
        mod.time.monotonic, mod.time.time = saved

    if fails:
        print(f"T4 FAIL ({len(fails)}):")
        for f in fails:
            print("  -", f)
        return 1
    print("T4 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
