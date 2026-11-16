"""Notification outbox: atomic enqueue, at-least-once delivery, no write re-drive."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from helpers import wait_until

from stock_pilot import notifications, tasks
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import initialize_database, iso, utcnow
from stock_pilot.enums import TaskStatus
from stock_pilot.metrics import render_metrics
from stock_pilot.models import CreateTaskRequest


class RecordSender:
    def __init__(self) -> None:
        self.events: list[notifications.NotificationEvent] = []

    def send(self, event: notifications.NotificationEvent) -> None:
        self.events.append(event)


class FlakySender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, event: notifications.NotificationEvent) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("notification endpoint down")


class AlwaysFailSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, event: notifications.NotificationEvent) -> None:
        self.calls += 1
        raise RuntimeError("notification endpoint down")


def make_task(db_path: str) -> str:
    task = tasks.create_task(
        db_path,
        CreateTaskRequest(
            sku_id="SKU-TSHIRT-001",
            spec="白色-M",
            target_price=Decimal("59.00"),
            reason="outbox 测试",
        ),
    )
    return task.task_id


def outbox_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM outbox_events ORDER BY id").fetchall()
    finally:
        conn.close()


def scalar(db_path: str, sql: str) -> object:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "stock_pilot.db")
    initialize_database(path)
    return path


def test_terminal_update_enqueues_notification_in_same_transaction(db_path: str):
    task_id = make_task(db_path)

    tasks.update_task(
        db_path,
        task_id,
        status=TaskStatus.VERIFIED,
        final_price=Decimal("59.00"),
    )

    rows = outbox_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["topic"] == "task.terminal"
    assert row["task_id"] == task_id
    assert row["delivered_at"] is None
    payload = json.loads(row["payload_json"])
    assert payload["status"] == "verified"
    assert payload["final_price_cents"] == 5900
    assert payload["sku_id"] == "SKU-TSHIRT-001"


def test_non_terminal_update_does_not_enqueue(db_path: str):
    task_id = make_task(db_path)

    tasks.update_task(db_path, task_id, status=TaskStatus.AWAITING_APPROVAL)
    tasks.update_task(db_path, task_id, listing_id="LST-001")

    assert outbox_rows(db_path) == []


def test_repeated_terminal_updates_enqueue_once(db_path: str):
    task_id = make_task(db_path)

    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)

    rows = outbox_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == f"task-terminal:{task_id}:verified"


def test_enqueue_failure_rolls_back_the_task_update(
    db_path: str, monkeypatch: pytest.MonkeyPatch
):
    task_id = make_task(db_path)

    def boom(tx, task_id):  # noqa: ANN001 - monkeypatched hook
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(notifications, "enqueue_terminal", boom)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)

    task = tasks.get_task(db_path, task_id)
    assert task is not None
    assert task.status is TaskStatus.RECEIVED
    assert outbox_rows(db_path) == []


def test_delivery_marks_delivered_once_and_never_writes_tools(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)
    sender = RecordSender()

    assert notifications.deliver_pending(db_path, sender) == 1
    assert len(sender.events) == 1
    assert sender.events[0].task_id == task_id
    assert outbox_rows(db_path)[0]["delivered_at"] is not None
    assert notifications.pending_count(db_path) == 0

    assert notifications.deliver_pending(db_path, sender) == 0
    assert len(sender.events) == 1

    # The worker has no tool client and must never touch the mall/inventory.
    assert scalar(db_path, "SELECT COUNT(*) FROM tool_events") == 0
    assert scalar(db_path, "SELECT price_cents FROM listings WHERE id='LST-001'") == 7900


def test_sender_failure_keeps_event_pending_for_retry(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.ABORTED_NO_STOCK)
    sender = FlakySender()

    assert notifications.deliver_pending(db_path, sender) == 0
    row = outbox_rows(db_path)[0]
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert "down" in row["last_error"]

    assert notifications.deliver_pending(db_path, sender) == 1
    row = outbox_rows(db_path)[0]
    assert row["delivered_at"] is not None
    assert row["attempts"] == 2
    assert row["last_error"] is None
    assert sender.calls == 2


def test_pending_metric_reflects_undelivered_events(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)

    text = render_metrics(db_path).decode()
    assert "stock_pilot_outbox_pending 1.0" in text

    notifications.deliver_pending(db_path, RecordSender())
    assert "stock_pilot_outbox_pending 0.0" in render_metrics(db_path).decode()


def test_exhausted_retries_dead_letter_the_event(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)
    sender = AlwaysFailSender()

    assert notifications.deliver_pending(db_path, sender, max_attempts=2) == 0
    assert notifications.deliver_pending(db_path, sender, max_attempts=2) == 0

    row = outbox_rows(db_path)[0]
    assert row["delivered_at"] is None
    assert row["dead_lettered_at"] is not None
    assert row["attempts"] == 2
    assert "down" in row["last_error"]
    assert notifications.pending_count(db_path) == 0
    assert notifications.dead_count(db_path) == 1

    # Dead events are excluded from later delivery passes.
    assert notifications.deliver_pending(db_path, sender, max_attempts=2) == 0
    assert sender.calls == 2

    text = render_metrics(db_path).decode()
    assert "stock_pilot_outbox_pending 0.0" in text
    assert "stock_pilot_outbox_dead 1.0" in text


def test_worker_applies_attempt_budget(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)
    worker = notifications.OutboxWorker(
        db_path,
        sender=AlwaysFailSender(),
        interval_seconds=0.05,
        max_attempts=1,
        prune_interval_seconds=0,
    )

    assert worker.run_once() == 0
    assert notifications.dead_count(db_path) == 1
    assert notifications.pending_count(db_path) == 0


def test_prune_removes_only_aged_delivered_and_dead_rows(db_path: str):
    first = make_task(db_path)
    second = make_task(db_path)
    third = make_task(db_path)
    tasks.update_task(db_path, first, status=TaskStatus.VERIFIED)
    tasks.update_task(db_path, second, status=TaskStatus.ROLLED_BACK)
    tasks.update_task(db_path, third, status=TaskStatus.VERIFIED)
    notifications.deliver_pending(db_path, RecordSender())

    old = iso(utcnow() - timedelta(days=8))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE outbox_events SET delivered_at = ? WHERE task_id = ?",
            (old, first),
        )
        conn.execute(
            "UPDATE outbox_events SET delivered_at = NULL,"
            " dead_lettered_at = ?, attempts = 5 WHERE task_id = ?",
            (old, second),
        )
        conn.commit()
    finally:
        conn.close()

    # A pending row must survive pruning even if it is old.
    fourth = make_task(db_path)
    tasks.update_task(db_path, fourth, status=TaskStatus.ABORTED_NO_STOCK)

    assert notifications.prune(db_path, retention_seconds=3600) == 2

    remaining = {row["task_id"] for row in outbox_rows(db_path)}
    assert remaining == {third, fourth}
    assert notifications.pending_count(db_path) == 1
    assert notifications.dead_count(db_path) == 0


def test_prune_is_a_no_op_for_a_young_outbox(db_path: str):
    task_id = make_task(db_path)
    tasks.update_task(db_path, task_id, status=TaskStatus.VERIFIED)
    notifications.deliver_pending(db_path, RecordSender())

    assert notifications.prune(db_path, retention_seconds=3600) == 0
    assert len(outbox_rows(db_path)) == 1


def test_initialization_migrates_an_old_outbox_table(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE outbox_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " dedupe_key TEXT NOT NULL UNIQUE,"
            " topic TEXT NOT NULL,"
            " task_id TEXT,"
            " payload_json TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " delivered_at TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " last_error TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    initialize_database(path, seed=False)

    conn = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(outbox_events)")
        }
    finally:
        conn.close()
    assert "dead_lettered_at" in columns


def test_api_lifespan_worker_delivers_terminal_events(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "stock_pilot.db"),
        outbox_interval_seconds=0.05,
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/tasks",
            json={
                "sku_id": "SKU-HOODIE-003",
                "spec": "黑色-L",
                "target_price": "99.00",
                "reason": "outbox 生命周期测试",
            },
        )
        assert created.status_code == 201
        assert created.json()["status"] == "aborted_no_stock"

        assert wait_until(
            lambda: notifications.pending_count(settings.db_path) == 0,
            timeout=10,
        )
        row = outbox_rows(settings.db_path)[0]
        assert row["delivered_at"] is not None
        assert json.loads(row["payload_json"])["status"] == "aborted_no_stock"
        assert "stock_pilot_outbox_pending 0.0" in client.get("/metrics").text
