"""Process-local intake limiter: bounded cost, honest retry semantics."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from stock_pilot.ratelimit import SlidingWindowLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_the_budget_then_reports_retry_after():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)

    assert limiter.check("agent") == (True, 0)
    clock.advance(2)
    assert limiter.check("agent") == (True, 0)
    allowed, retry_after = limiter.check("agent")

    assert allowed is False
    assert retry_after == 58  # oldest accepted attempt was two seconds ago


def test_rejected_attempts_do_not_extend_the_lockout():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    assert limiter.check("agent")[0] is True

    clock.advance(30)
    assert limiter.check("agent")[0] is False
    clock.advance(30)
    # The rejected attempt at +30 was not recorded; the original expires.
    assert limiter.check("agent")[0] is True


def test_identity_buckets_are_isolated_and_zero_disables():
    limiter = SlidingWindowLimiter(1, 60)
    assert limiter.check("agent")[0] is True
    assert limiter.check("agent")[0] is False
    assert limiter.check("admin")[0] is True

    disabled = SlidingWindowLimiter(0, 60)
    assert all(disabled.check("agent")[0] for _ in range(50))


def test_concurrent_checks_never_exceed_the_budget():
    limiter = SlidingWindowLimiter(5, 60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(limiter.check, ["agent"] * 20))

    assert sum(1 for allowed, _ in results if allowed) == 5


@pytest.mark.parametrize(
    ("limit", "window"),
    [(-1, 60.0), (1, 0.0), (1, -1.0)],
)
def test_invalid_limiter_configuration_is_rejected(limit: int, window: float):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit, window)
