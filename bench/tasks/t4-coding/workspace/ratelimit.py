"""A token-bucket rate limiter. See TASK.md for the full contract."""

import time


class TokenBucket:
    def __init__(self, capacity, refill_rate, clock=None):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.clock = clock or time.monotonic
        self.tokens = capacity

    def try_acquire(self, n=1.0):
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def acquire(self, n=1.0, timeout=None):
        while not self.try_acquire(n):
            time.sleep(0.01)
        return True

    def available(self):
        return self.tokens
