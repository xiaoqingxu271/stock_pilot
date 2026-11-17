"""Process-local sliding-window rate limiting for the optional intake edge.

This protects the only endpoint that can spend money on an upstream model. It
is deliberately in-memory and per-process: it is a cost guard for a demo or a
single instance, not a distributed quota. Multi-instance deployments must put
the real limit in a shared gateway or store.

Only accepted attempts are recorded. Recording rejected attempts would let a
client that keeps hammering extend its own lockout indefinitely.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Allow at most ``limit`` attempts per identity in a rolling window.

    ``limit == 0`` disables limiting. ``clock`` is injectable so tests can move
    time without sleeping.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 0:
            raise ValueError("limit must not be negative")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record one attempt and return ``(allowed, retry_after_seconds)``.

        A rejected attempt is not recorded, so the client recovers exactly one
        window after its oldest accepted attempt.
        """
        if self.limit == 0:
            return True, 0

        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.limit:
                retry_after = self.window_seconds - (now - attempts[0])
                return False, max(1, math.ceil(retry_after))
            attempts.append(now)
            return True, 0
