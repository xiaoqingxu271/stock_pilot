"""Timeout / retry / circuit-breaker policy tests."""

from __future__ import annotations

import time

import pytest

from stock_pilot.config import Settings
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import CircuitOpenError, ToolFailure, ToolTimeoutError
from stock_pilot.runner import ToolExecutor


def make_settings(
    *,
    timeout: float = 0.1,
    retries: int = 0,
    threshold: int = 10,
    cooldown: float = 0.5,
) -> Settings:
    return Settings(
        db_path=":memory:",
        tool_timeout_seconds=timeout,
        read_max_retries=retries,
        circuit_failure_threshold=threshold,
        circuit_cooldown_seconds=cooldown,
    )


def test_write_timeout_is_attempted_once():
    calls: list[int] = []

    def slow() -> None:
        calls.append(1)
        time.sleep(0.5)

    executor = ToolExecutor(make_settings(timeout=0.05))
    with pytest.raises(ToolTimeoutError):
        executor.call_tool("slow_write", slow, is_write=True)
    assert len(calls) == 1


def test_read_timeout_retries_then_circuit_opens():
    calls: list[int] = []

    def slow() -> None:
        calls.append(1)
        time.sleep(0.5)

    executor = ToolExecutor(
        make_settings(timeout=0.05, retries=2, threshold=2, cooldown=5.0)
    )
    with pytest.raises(CircuitOpenError):
        executor.call_tool("slow_read", slow)
    # Attempt 1 failed, attempt 2 failed and opened the circuit,
    # attempt 3 was blocked before submission.
    assert len(calls) == 2

    with pytest.raises(CircuitOpenError):
        executor.call_tool("slow_read", slow)
    assert len(calls) == 2


def test_circuit_cooldown_allows_half_open_probe():
    def slow() -> None:
        time.sleep(0.5)

    executor = ToolExecutor(
        make_settings(timeout=0.05, retries=0, threshold=1, cooldown=0.05)
    )
    with pytest.raises(ToolTimeoutError):
        executor.call_tool("slow_read", slow)
    with pytest.raises(CircuitOpenError):
        executor.call_tool("slow_read", slow)

    time.sleep(0.1)
    with pytest.raises(ToolTimeoutError):
        executor.call_tool("slow_read", slow)


def test_business_failure_is_not_retried_or_counted_by_circuit():
    calls: list[int] = []

    def failing() -> None:
        calls.append(1)
        raise ToolFailure(ErrorCode.APPROVAL_REJECTED, "rejected")

    executor = ToolExecutor(
        make_settings(timeout=1.0, retries=2, threshold=1, cooldown=5.0)
    )
    for _ in range(5):
        with pytest.raises(ToolFailure):
            executor.call_tool("business_fail", failing)
    assert len(calls) == 5

    assert executor.call_tool("healthy", lambda: 42) == 42


def test_read_retry_recovers_on_second_attempt():
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.2)
        return "ok"

    executor = ToolExecutor(make_settings(timeout=0.05, retries=1, threshold=10))
    assert executor.call_tool("flaky_read", flaky) == "ok"
    assert len(calls) == 2
