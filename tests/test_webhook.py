"""Webhook delivery and the read/retry surface for dead-lettered notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from stock_pilot import notifications, tasks
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import utcnow
from stock_pilot.enums import NotificationTopic, TaskStatus
from stock_pilot.models import CreateTaskRequest
from stock_pilot.notifications import NotificationEvent, WebhookSender
from stock_pilot.tools import StockPilotTools


class CaptureHandler(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, str], bytes]] = []
    status = 204

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append((dict(self.headers), body))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def webhook_server():
    CaptureHandler.received = []
    CaptureHandler.status = 204
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/notify", CaptureHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class AlwaysFailSender:
    def send(self, event: NotificationEvent) -> None:
        raise RuntimeError("notification endpoint down")


class RecordSender:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def send(self, event: NotificationEvent) -> None:
        self.events.append(event)


def event() -> NotificationEvent:
    return NotificationEvent(
        event_id=1,
        dedupe_key="task-terminal:task_1:verified",
        topic=NotificationTopic.TASK_TERMINAL,
        task_id="task_1",
        payload={"task_id": "task_1", "status": "verified"},
        attempts=0,
        created_at=utcnow(),
    )


def test_webhook_signs_the_body_and_sends_the_event(webhook_server):
    url, handler = webhook_server
    sender = WebhookSender(url, secret="s3cret", timeout_seconds=2)

    sender.send(event())

    headers, body = handler.received[0]
    headers = {key.lower(): value for key, value in headers.items()}
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["x-stockpilot-signature"] == f"sha256={expected}"
    decoded = json.loads(body)
    assert decoded["dedupe_key"] == "task-terminal:task_1:verified"
    assert decoded["payload"]["status"] == "verified"


def test_webhook_failure_omits_the_url_from_the_error(webhook_server):
    url, handler = webhook_server
    handler.status = 500
    sender = WebhookSender(url, timeout_seconds=2)

    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        sender.send(event())
    assert url not in str(exc.value)


def test_create_sender_prefers_the_configured_webhook(tmp_path):
    default = notifications.create_sender(
        Settings(db_path=str(tmp_path / "a.db"))
    )
    assert isinstance(default, notifications.LogSender)

    configured = notifications.create_sender(
        Settings(
            db_path=str(tmp_path / "b.db"),
            webhook_url="http://127.0.0.1:9/notify",
            webhook_secret="secret",
        )
    )
    assert isinstance(configured, WebhookSender)


def test_webhook_settings_are_validated(tmp_path):
    with pytest.raises(ValueError, match="webhook_url"):
        Settings(db_path=str(tmp_path / "a.db"), webhook_url="not-a-url")

    settings = Settings(
        db_path=str(tmp_path / "b.db"),
        webhook_url="https://example.invalid/notify",
        webhook_secret="not-logged",
    )
    assert "not-logged" not in repr(settings)


def test_deliver_pending_can_use_the_webhook_sender(webhook_server, tmp_path):
    url, handler = webhook_server
    settings = Settings(
        db_path=str(tmp_path / "stock_pilot.db"),
        outbox_interval_seconds=3600,
    )
    StockPilotTools(settings)
    task = tasks.create_task(
        settings.db_path,
        CreateTaskRequest(
            sku_id="SKU-TSHIRT-001",
            spec="白色-M",
            target_price=Decimal("59.00"),
            reason="webhook 测试",
        ),
    )
    tasks.update_task(settings.db_path, task.task_id, status=TaskStatus.VERIFIED)

    assert (
        notifications.deliver_pending(
            settings.db_path,
            WebhookSender(url, timeout_seconds=2),
            max_attempts=2,
        )
        == 1
    )
    assert len(handler.received) == 1
    assert notifications.pending_count(settings.db_path) == 0


def make_terminal_task(settings: Settings) -> str:
    StockPilotTools(settings)
    task = tasks.create_task(
        settings.db_path,
        CreateTaskRequest(
            sku_id="SKU-TSHIRT-001",
            spec="白色-M",
            target_price=Decimal("59.00"),
            reason="outbox 运维测试",
        ),
    )
    tasks.update_task(settings.db_path, task.task_id, status=TaskStatus.VERIFIED)
    return task.task_id


def test_dead_letters_are_listed_and_retried_with_operator_auth(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "stock_pilot.db"),
        operator_token="op-token",
        outbox_interval_seconds=3600,
    )
    make_terminal_task(settings)
    assert (
        notifications.deliver_pending(
            settings.db_path, AlwaysFailSender(), max_attempts=1
        )
        == 0
    )
    assert notifications.dead_count(settings.db_path) == 1

    app = create_app(settings)
    with TestClient(app) as client:
        dead = client.get("/outbox", params={"status": "dead"})
        assert dead.status_code == 200
        assert len(dead.json()) == 1
        event_id = dead.json()[0]["event_id"]

        assert client.get("/outbox", params={"status": "pending"}).json() == []

        denied = client.post(f"/outbox/{event_id}/retry")
        assert denied.status_code == 401

        retried = client.post(
            f"/outbox/{event_id}/retry",
            headers={"Authorization": "Bearer op-token"},
        )
        assert retried.status_code == 200
        assert retried.json()["dead_lettered_at"] is None
        assert retried.json()["attempts"] == 0
        assert retried.json()["last_error"] is None

        assert len(client.get("/outbox", params={"status": "pending"}).json()) == 1

        assert (
            client.post(
                f"/outbox/{event_id}/retry",
                headers={"Authorization": "Bearer op-token"},
            ).status_code
            == 200
        )
        assert notifications.pending_count(settings.db_path) == 1


def test_retry_rejects_delivered_and_missing_events(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "stock_pilot.db"),
        outbox_interval_seconds=3600,
    )
    make_terminal_task(settings)
    notifications.deliver_pending(settings.db_path, RecordSender())
    event_id = notifications.list_events(settings.db_path)[0].event_id

    with TestClient(create_app(settings)) as client:
        delivered = client.post(f"/outbox/{event_id}/retry")
        missing = client.post("/outbox/999999/retry")

    assert delivered.status_code == 409
    assert missing.status_code == 404


def test_retry_does_not_touch_the_mall_or_the_tool_audit(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "stock_pilot.db"),
        outbox_interval_seconds=3600,
    )
    make_terminal_task(settings)
    notifications.deliver_pending(
        settings.db_path, AlwaysFailSender(), max_attempts=1
    )
    event_id = notifications.list_events(settings.db_path)[0].event_id

    with TestClient(create_app(settings)) as client:
        assert client.post(f"/outbox/{event_id}/retry").status_code == 200

    conn = sqlite3.connect(settings.db_path)
    try:
        price = conn.execute(
            "SELECT price_cents FROM listings WHERE id = 'LST-001'"
        ).fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0]
    finally:
        conn.close()
    assert price == 7900
    assert events == 0
