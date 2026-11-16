"""Tool execution policy: hard timeout, bounded retries, circuit breaker.

Reads may retry; writes are attempted exactly once. A write timeout is
reported as ``ToolTimeoutError`` so the caller can perform a single
read-only reconciliation and then stop — never continue with more writes.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from stock_pilot.config import Settings
from stock_pilot.errors import (
    CircuitOpenError,
    ToolExecutionError,
    ToolFailure,
    ToolTimeoutError,
    ToolUnavailableError,
)

T = TypeVar("T")

_DEFAULT_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="stock-pilot-tool"
)


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class ToolExecutor:
    def __init__(
        self,
        settings: Settings,
        executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._executor = executor or _DEFAULT_EXECUTOR
        self._lock = threading.Lock()
        self._circuits: dict[str, _CircuitState] = {}

    def _state(self, name: str) -> _CircuitState:
        with self._lock:
            return self._circuits.setdefault(name, _CircuitState())

    def _ensure_closed(self, name: str) -> None:
        state = self._state(name)
        with self._lock:
            if state.opened_at is None:
                return
            elapsed = time.monotonic() - state.opened_at
            if elapsed < self._settings.circuit_cooldown_seconds:
                raise CircuitOpenError(
                    f"circuit open for tool '{name}' "
                    f"({elapsed:.1f}s of "
                    f"{self._settings.circuit_cooldown_seconds:.1f}s cooldown)"
                )
            # Cooldown elapsed: allow a half-open probe.
            state.failures = 0
            state.opened_at = None

    def _record_success(self, name: str) -> None:
        state = self._state(name)
        with self._lock:
            state.failures = 0
            state.opened_at = None

    def _record_failure(self, name: str) -> None:
        state = self._state(name)
        with self._lock:
            state.failures += 1
            if state.failures >= self._settings.circuit_failure_threshold:
                state.opened_at = time.monotonic()

    def call_tool(
        self,
        name: str,
        fn: Callable[..., T],
        *args: Any,
        is_write: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        attempts = 1 if is_write else 1 + max(0, self._settings.read_max_retries)
        deadline = (
            timeout if timeout is not None else self._settings.tool_timeout_seconds
        )

        last_error: ToolTimeoutError | None = None
        for attempt in range(attempts):
            self._ensure_closed(name)
            try:
                future = self._executor.submit(fn, *args, **kwargs)
                result = future.result(timeout=deadline)
            except FuturesTimeoutError:
                self._record_failure(name)
                last_error = ToolTimeoutError(
                    f"tool '{name}' timed out after {deadline:.2f}s"
                )
                if attempt + 1 < attempts:
                    continue
                raise last_error
            except ToolFailure:
                # Deterministic business rule violation: retrying cannot help,
                # and it must not trip the transport circuit breaker.
                raise
            except ToolUnavailableError:
                # Transport down: retry reads, then surface as unavailable so
                # the orchestrator can stop without further writes.
                self._record_failure(name)
                if attempt + 1 < attempts:
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - converted to structured error
                self._record_failure(name)
                if attempt + 1 < attempts:
                    continue
                raise ToolExecutionError(f"tool '{name}' failed: {exc}", exc) from exc
            else:
                self._record_success(name)
                return result

        # Unreachable: loop either returns or raises.
        raise last_error or ToolTimeoutError(f"tool '{name}' failed")
