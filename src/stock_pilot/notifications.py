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

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from stock_pilot.config import Settings
from stock_pilot.db import connect, iso, parse_dt, transaction, utcnow
from stock_pilot.enums import (
    TERMINAL_TASK_STATUSES,
    ErrorCode,
    NotificationTopic,
    OutboxState,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import OutboxEventView

logger = logging.getLogger(__name__)

_MAX_ERROR_LENGTH = 500
DEFAULT_MAX_ATTEMPTS = 60
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60


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


class WebhookSender:
    """POST one notification event to an operator-configured endpoint.

    Delivery errors stay inside the outbox retry/dead-letter path; the raised
    messages omit the URL so a token embedded in it cannot reach a log. When a
    secret is configured the raw body is signed with HMAC-SHA256 and the
    signature is sent in ``X-StockPilot-Signature``.
    """

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.url = url
        self.secret = secret
        self.timeout_seconds = timeout_seconds

    def send(self, event: NotificationEvent) -> None:
        body = json.dumps(
            {
                "event_id": event.event_id,
                "dedupe_key": event.dedupe_key,
                "topic": event.topic.value,
                "task_id": event.task_id,
                "payload": event.payload,
                "created_at": iso(event.created_at),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "StockPilot-Outbox/0.1",
        }
        if self.secret:
            digest = hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            headers["X-StockPilot-Signature"] = f"sha256={digest}"
        request = urllib.request.Request(
            self.url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                if response.status >= 300:
                    raise RuntimeError(
                        f"webhook returned HTTP {response.status}"
                    )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"webhook returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("webhook delivery failed") from exc


def create_sender(settings: Settings) -> NotificationSender:
    """Use the webhook when configured; otherwise keep the local log sender."""
    if settings.webhook_url:
        return WebhookSender(
            settings.webhook_url,
            secret=settings.webhook_secret,
            timeout_seconds=settings.webhook_timeout_seconds,
        )
    return LogSender()


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


def state_counts(db_path: str) -> tuple[int, int, int]:
    """Return ``(pending, delivered, dead)`` from one read-only query.

    Dead events never carry ``delivered_at``, so the three buckets are
    disjoint. The console uses this as a compact delivery-health snapshot;
    metrics reuse the same derived counts.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN delivered_at IS NULL"
            "   AND dead_lettered_at IS NULL THEN 1 ELSE 0 END), 0) AS pending,"
            " COALESCE(SUM(CASE WHEN delivered_at IS NOT NULL"
            "   THEN 1 ELSE 0 END), 0) AS delivered,"
            " COALESCE(SUM(CASE WHEN dead_lettered_at IS NOT NULL"
            "   THEN 1 ELSE 0 END), 0) AS dead"
            " FROM outbox_events"
        ).fetchone()
    finally:
        conn.close()
    return int(row["pending"]), int(row["delivered"]), int(row["dead"])


def pending_count(db_path: str) -> int:
    return state_counts(db_path)[0]


def dead_count(db_path: str) -> int:
    return state_counts(db_path)[2]


_VIEW_COLUMNS = (
    "id, dedupe_key, topic, task_id, payload_json, created_at,"
    " delivered_at, dead_lettered_at, attempts, last_error"
)

_STATE_FILTERS = {
    OutboxState.PENDING: "delivered_at IS NULL AND dead_lettered_at IS NULL",
    OutboxState.DELIVERED: "delivered_at IS NOT NULL",
    OutboxState.DEAD: "dead_lettered_at IS NOT NULL",
}


def _view_from_row(row: sqlite3.Row) -> OutboxEventView:
    return OutboxEventView(
        event_id=row["id"],
        dedupe_key=row["dedupe_key"],
        topic=row["topic"],
        task_id=row["task_id"],
        payload=json.loads(row["payload_json"]),
        created_at=parse_dt(row["created_at"]),
        delivered_at=(
            parse_dt(row["delivered_at"]) if row["delivered_at"] else None
        ),
        dead_lettered_at=(
            parse_dt(row["dead_lettered_at"]) if row["dead_lettered_at"] else None
        ),
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


def list_events(
    db_path: str,
    *,
    status: OutboxState | None = None,
    limit: int = 100,
) -> list[OutboxEventView]:
    """Read the notification queue for operators; no delivery happens here."""
    sql = f"SELECT {_VIEW_COLUMNS} FROM outbox_events"
    params: list[Any] = []
    if status is not None:
        sql += f" WHERE {_STATE_FILTERS[status]}"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_view_from_row(row) for row in rows]


def get_event(db_path: str, event_id: int) -> OutboxEventView | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {_VIEW_COLUMNS} FROM outbox_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    return _view_from_row(row) if row is not None else None


def retry_dead(db_path: str, event_id: int) -> OutboxEventView:
    """Re-arm a dead-lettered event. Notification-only, never a write replay.

    Delivered events are immutable (409 at the API). A still-pending event is
    returned unchanged, so a repeated retry is idempotent.
    """
    with transaction(db_path) as tx:
        row = tx.execute(
            f"SELECT {_VIEW_COLUMNS} FROM outbox_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ToolFailure(
                ErrorCode.OUTBOX_EVENT_NOT_FOUND,
                f"outbox event {event_id} does not exist",
            )
        if row["delivered_at"] is not None:
            raise ToolFailure(
                ErrorCode.INVALID_STATE,
                "delivered notifications cannot be retried",
            )
        if row["dead_lettered_at"] is not None:
            tx.execute(
                "UPDATE outbox_events SET attempts = 0, last_error = NULL,"
                " dead_lettered_at = NULL WHERE id = ?",
                (event_id,),
            )
            row = tx.execute(
                f"SELECT {_VIEW_COLUMNS} FROM outbox_events WHERE id = ?",
                (event_id,),
            ).fetchone()
    assert row is not None
    return _view_from_row(row)


def prune(
    db_path: str, *, retention_seconds: int = DEFAULT_RETENTION_SECONDS
) -> int:
    """Delete delivered or dead-lettered rows older than the retention window.

    Pending rows are never touched: an undelivered notification stays until it
    is either delivered or dead-lettered. Only notification rows are pruned;
    task records and the audit trail keep their own lifecycle.
    """
    if retention_seconds <= 0:
        raise ValueError("retention_seconds must be positive")
    cutoff = iso(utcnow() - timedelta(seconds=retention_seconds))
    with transaction(db_path) as tx:
        cursor = tx.execute(
            "DELETE FROM outbox_events WHERE"
            " (delivered_at IS NOT NULL AND delivered_at < ?)"
            " OR (dead_lettered_at IS NOT NULL AND dead_lettered_at < ?)",
            (cutoff, cutoff),
        )
        return cursor.rowcount


def deliver_pending(
    db_path: str,
    sender: NotificationSender,
    *,
    limit: int = 50,
    max_attempts: int | None = None,
) -> int:
    """Deliver pending events; returns how many were marked delivered.

    At-least-once: the delivery call happens before the row is marked, so a
    crash can replay one event. Sender failures leave the row pending with an
    incremented attempt count for the next pass. With ``max_attempts`` set, an
    event that exhausts them is dead-lettered instead of retried forever; it
    is excluded from later passes but stays visible to ``dead_count``.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, dedupe_key, topic, task_id, payload_json, attempts,"
            " created_at FROM outbox_events"
            " WHERE delivered_at IS NULL AND dead_lettered_at IS NULL"
            " ORDER BY id LIMIT ?",
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
            attempts = event.attempts + 1
            exhausted = max_attempts is not None and attempts >= max_attempts
            logger.warning(
                "notification delivery failed for event %s (attempt %d)%s: %s",
                event.event_id,
                attempts,
                ", dead-lettering" if exhausted else "",
                exc,
            )
            with transaction(db_path) as tx:
                if exhausted:
                    tx.execute(
                        "UPDATE outbox_events SET attempts = ?, last_error = ?,"
                        " dead_lettered_at = ? WHERE id = ?"
                        " AND delivered_at IS NULL"
                        " AND dead_lettered_at IS NULL",
                        (
                            attempts,
                            str(exc)[:_MAX_ERROR_LENGTH],
                            iso(utcnow()),
                            event.event_id,
                        ),
                    )
                else:
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
        max_attempts: int | None = DEFAULT_MAX_ATTEMPTS,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        prune_interval_seconds: float = 3600.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_attempts is not None and max_attempts <= 0:
            raise ValueError("max_attempts must be positive or None")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if prune_interval_seconds < 0:
            raise ValueError("prune_interval_seconds must not be negative")
        self.db_path = db_path
        self.sender = sender or LogSender()
        self.interval_seconds = interval_seconds
        self.max_attempts = max_attempts
        self.retention_seconds = retention_seconds
        self.prune_interval_seconds = prune_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_prune: float | None = None

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
        delivered = deliver_pending(
            self.db_path, self.sender, max_attempts=self.max_attempts
        )
        self._maybe_prune()
        return delivered

    def _maybe_prune(self) -> None:
        now = time.monotonic()
        if (
            self._last_prune is not None
            and now - self._last_prune < self.prune_interval_seconds
        ):
            return
        self._last_prune = now
        try:
            removed = prune(
                self.db_path, retention_seconds=self.retention_seconds
            )
        except Exception:  # noqa: BLE001 - pruning is maintenance, not delivery
            logger.exception("outbox retention pass failed")
            return
        if removed:
            logger.info("pruned %d delivered/dead notification event(s)", removed)

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
