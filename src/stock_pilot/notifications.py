"""Notification-only outbox.

A terminal task update and its notification event are written in the same
transaction, then a background worker delivers pending events at least once.
The worker *never* holds a tool client and never calls a write tool: this is
the deliberate alternative to an outbox that would re-drive writes (ADR 0005,
ADR 0007). A crash after sending but before marking delivered produces a
duplicate notification, which downstream consumers dedupe by ``dedupe_key``.

The table lives in the orchestrator store even in split layouts, because task
state and its notification must share one transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from stock_pilot.db import connect, iso, parse_dt, transaction, utcnow
from stock_pilot.enums import (
    TERMINAL_TASK_STATUSES,
    NotificationTopic,
    TaskStatus,
)

logger = logging.getLogger(__name__)

_MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class NotificationEvent:
    event_id: int
    dedupe_key: str
    topic: NotificationTopic
    task_id: str | None
    payload: dict[str, Any]
    attempts: int
    created_at: datetime


class NotificationSender(Protocol):
    def send(self, event: NotificationEvent) -> None: ...


class LogSender:
    """Default sender: one structured log line per event, no external I/O."""

    def send(self, event: NotificationEvent) -> None:
        logger.info(
            "notification %s task=%s dedupe=%s payload=%s",
            event.topic.value,
            event.task_id,
            event.dedupe_key,
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
        )


def enqueue_terminal(tx: sqlite3.Connection, task_id: str) -> None:
    """Enqueue a terminal-task notification inside the caller's transaction.

    Idempotent: repeated terminal updates for the same task/status keep the
    first event (``dedupe_key`` is unique). Non-terminal statuses are ignored.
    """
    row = tx.execute(
        "SELECT id, sku_id, spec, status, listing_id, approval_id,"
        " final_price_cents, error_code FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    status = TaskStatus(row["status"])
    if status not in TERMINAL_TASK_STATUSES:
        return

    payload = {
        "task_id": row["id"],
        "sku_id": row["sku_id"],
        "spec": row["spec"],
        "status": status.value,
        "listing_id": row["listing_id"],
        "approval_id": row["approval_id"],
        "final_price_cents": row["final_price_cents"],
        "error_code": row["error_code"],
    }
    dedupe_key = f"task-terminal:{row['id']}:{status.value}"
    tx.execute(
        "INSERT OR IGNORE INTO outbox_events"
        " (dedupe_key, topic, task_id, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            dedupe_key,
            NotificationTopic.TASK_TERMINAL.value,
            row["id"],
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            iso(utcnow()),
        ),
    )


def pending_count(db_path: str) -> int:
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM outbox_events WHERE delivered_at IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()


def deliver_pending(
    db_path: str,
    sender: NotificationSender,
    *,
    limit: int = 50,
) -> int:
    """Deliver pending events; returns how many were marked delivered.

    At-least-once: the delivery call happens before the row is marked, so a
    crash can replay one event. Sender failures leave the row pending with an
    incremented attempt count for the next pass.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, dedupe_key, topic, task_id, payload_json, attempts,"
            " created_at FROM outbox_events"
            " WHERE delivered_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    delivered = 0
    for row in rows:
        event = NotificationEvent(
            event_id=row["id"],
            dedupe_key=row["dedupe_key"],
            topic=NotificationTopic(row["topic"]),
            task_id=row["task_id"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"],
            created_at=parse_dt(row["created_at"]),
        )
        try:
            sender.send(event)
        except Exception as exc:  # noqa: BLE001 - retried on the next pass
            logger.warning(
                "notification delivery failed for event %s: %s",
                event.event_id,
                exc,
            )
            with transaction(db_path) as tx:
                tx.execute(
                    "UPDATE outbox_events SET attempts = attempts + 1,"
                    " last_error = ? WHERE id = ? AND delivered_at IS NULL",
                    (str(exc)[:_MAX_ERROR_LENGTH], event.event_id),
                )
            continue

        with transaction(db_path) as tx:
            tx.execute(
                "UPDATE outbox_events SET delivered_at = ?,"
                " attempts = attempts + 1, last_error = NULL"
                " WHERE id = ? AND delivered_at IS NULL",
                (iso(utcnow()), event.event_id),
            )
        delivered += 1
    return delivered


class OutboxWorker:
    """Polls the outbox and hands events to a sender. No tool access by design."""

    def __init__(
        self,
        db_path: str,
        *,
        sender: NotificationSender | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self.db_path = db_path
        self.sender = sender or LogSender()
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="stock-pilot-outbox",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None

    def run_once(self) -> int:
        return deliver_pending(self.db_path, self.sender)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a failed pass retries next tick
                logger.exception("outbox delivery pass failed")

    def __enter__(self) -> OutboxWorker:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
