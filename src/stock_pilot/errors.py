"""Internal exception types.

Business-rule violations are raised as :class:`ToolFailure` with an
:class:`~stock_pilot.enums.ErrorCode`. Transport/execution failures
(timeouts, open circuit) use their own types so callers can apply the
"state unknown, stop writing" rule.
"""

from __future__ import annotations

from stock_pilot.enums import ErrorCode


class ToolFailure(Exception):
    """A deterministic, business-level failure (no retry should be attempted)."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolTimeoutError(Exception):
    """The tool call exceeded its deadline. Outcome is unknown."""


class ToolUnavailableError(Exception):
    """The tool transport is unavailable (e.g. the MCP session is down)."""


class CircuitOpenError(Exception):
    """The circuit breaker is open; the call was not attempted."""


class ToolExecutionError(Exception):
    """Unexpected failure while executing a tool."""

    def __init__(self, message: str, original: BaseException | None = None) -> None:
        super().__init__(message)
        self.original = original
