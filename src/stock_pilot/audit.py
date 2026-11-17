"""Tool-call audit trail and task correlation context.

The graph sets a task context around each invocation; ``ToolExecutor`` reads
it when a call finishes and appends one row per attempt. Writes are
best-effort: an audit failure must never turn a successful tool call into a
task failure.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from stock_pilot.db import connect, iso, parse_dt, transaction, utcnow
from stock_pilot.enums import ErrorCode, ToolOutcome
from stock_pilot.models import ToolEvent

logger = logging.getLogger(__name__)

_current_task_id: ContextVar[str | None] = ContextVar(
    "stock_pilot_current_task_id", default=None
)

EventSink = Callable[[str, int, ToolOutcome, ErrorCode | None, int], None]


@contextmanager
def task_context(task_id: str) -> Iterator[None]:
    token = _current_task_id.set(task_id)
    try:
        yield
    finally:
        _current_task_id.reset(token)


def current_task_id() -> str | None:
    return _current_task_id.get()


def record_tool_event(
    db_path: str,
    *,
    tool: str,
    attempt: int,
    outcome: ToolOutcome,
    error_code: ErrorCode | None,
    latency_ms: int,
) -> None:
    with transaction(db_path) as tx:
        tx.execute(
            "INSERT INTO tool_events (task_id, tool, attempt, outcome, error_code,"
            " latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                current_task_id(),
                tool,
                attempt,
                outcome.value,
                error_code.value if error_code is not None else None,
                max(0, int(latency_ms)),
                iso(utcnow()),
            ),
        )


def make_event_sink(db_path: str) -> EventSink:
    def sink(
        tool: str,
        attempt: int,
        outcome: ToolOutcome,
        error_code: ErrorCode | None,
        latency_ms: int,
    ) -> None:
        try:
            record_tool_event(
                db_path,
                tool=tool,
                attempt=attempt,
                outcome=outcome,
                error_code=error_code,
                latency_ms=latency_ms,
            )
        except Exception:  # noqa: BLE001 - audit must not break the task
            logger.debug("failed to record tool event for %s", tool, exc_info=True)

    return sink


def tool_event_from_row(row: sqlite3.Row) -> ToolEvent:
    return ToolEvent(
        event_id=row["id"],
        task_id=row["task_id"],
        tool=row["tool"],
        attempt=row["attempt"],
        outcome=ToolOutcome(row["outcome"]),
        error_code=(
            ErrorCode(row["error_code"]) if row["error_code"] is not None else None
        ),
        latency_ms=row["latency_ms"],
        created_at=parse_dt(row["created_at"]),
    )


def list_tool_events(db_path: str, task_id: str) -> list[ToolEvent]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tool_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [tool_event_from_row(row) for row in rows]
