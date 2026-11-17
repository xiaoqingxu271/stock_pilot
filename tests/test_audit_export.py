"""Audit export: one read-only snapshot, no tools, no credentials, no writes."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import utcnow
from stock_pilot.enums import NotificationTopic


class StubLLM:
    def extract(self, text: str) -> dict[str, Any]:
        return {
            "sku_id": "SKU-TSHIRT-001",
            "spec": "白色-M",
            "target_price": 59,
            "reason": "库存驱动调价",
        }


def payload(reason: str = "库存驱动调价") -> dict[str, str]:
    return {
        "sku_id": "SKU-TSHIRT-001",
        "spec": "白色-M",
        "target_price": "59.00",
        "reason": reason,
    }


def authenticate(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def table_counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "approvals", "staging", "tool_events", "outbox_events")
        }
    finally:
        conn.close()


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def reject(client: TestClient, task: dict[str, Any]) -> None:
    response = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "reject", "note": "库存策略调整"},
    )
    assert response.status_code == 200


def test_audit_export_contains_the_full_trail_and_writes_nothing(client, settings: Settings):
    task = client.post("/tasks", json=payload()).json()
    reject(client, task)

    before = table_counts(settings.db_path)
    response = client.get("/audit/export")
    after = table_counts(settings.db_path)

    assert response.status_code == 200
    body = response.json()
    assert body["since"] is None and body["until"] is None
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["task_id"] == task["task_id"]
    assert body["tasks"][0]["status"] == "rolled_back"

    assert len(body["approvals"]) == 1
    assert body["approvals"][0]["approval_id"] == task["approval_id"]
    assert body["approvals"][0]["status"] == "rejected"
    assert body["approvals"][0]["decided_by"] is None

    assert {event["tool"] for event in body["tool_events"]} >= {
        "check_inventory",
        "get_listing",
        "update_price",
    }
    assert all(event["task_id"] == task["task_id"] for event in body["tool_events"])

    assert len(body["outbox"]) == 1
    notification = body["outbox"][0]
    assert notification["topic"] == NotificationTopic.TASK_TERMINAL.value
    assert notification["task_id"] == task["task_id"]
    assert notification["attempts"] >= 0
    # Payload bodies and delivery error text are not part of the export.
    assert set(notification) == {
        "event_id",
        "dedupe_key",
        "topic",
        "task_id",
        "created_at",
        "delivered_at",
        "dead_lettered_at",
        "attempts",
    }

    assert before == after


def test_audit_export_requires_the_operator_role(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "pilot.db"),
        agent_token="agent-token",
        operator_token="operator-token",
        admin_token="admin-token",
    )
    with TestClient(create_app(settings)) as client:
        missing = client.get("/audit/export")
        agent = client.get("/audit/export", headers=authenticate("agent-token"))
        operator = client.get("/audit/export", headers=authenticate("operator-token"))
        admin = client.get("/audit/export", headers=authenticate("admin-token"))

    assert missing.status_code == 401
    assert agent.status_code == 403
    assert operator.status_code == 200
    assert admin.status_code == 200


def test_audit_export_applies_the_time_window_and_limit(client):
    first = client.post("/tasks", json=payload("第一单")).json()
    second = client.post("/tasks", json=payload("第二单")).json()

    now = utcnow()
    future = (now + timedelta(days=1)).isoformat()
    past = (now - timedelta(days=1)).isoformat()

    future_export = client.get("/audit/export", params={"since": future})
    past_export = client.get("/audit/export", params={"until": past})
    limited = client.get("/audit/export", params={"limit": 1})

    assert future_export.status_code == 200
    assert future_export.json()["tasks"] == []
    assert future_export.json()["approvals"] == []
    assert future_export.json()["tool_events"] == []
    assert future_export.json()["outbox"] == []

    assert past_export.json()["tasks"] == []
    assert past_export.json()["approvals"] == []

    limited_body = limited.json()
    assert limited.status_code == 200
    assert len(limited_body["tasks"]) == 1
    assert len(limited_body["approvals"]) == 1
    # The limit is per collection and never exceeds what was requested.
    assert limited_body["tasks"][0]["task_id"] in {
        first["task_id"],
        second["task_id"],
    }


def test_audit_export_rejects_an_inverted_window(client):
    now = utcnow()
    response = client.get(
        "/audit/export",
        params={
            "since": (now + timedelta(days=1)).isoformat(),
            "until": (now - timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 400
    assert "since" in response.json()["detail"]


def test_audit_export_never_returns_credentials(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "pilot.db"),
        agent_token="agent-secret-token",
        operator_token="operator-secret-token",
        admin_token="admin-secret-token",
        llm_provider="anthropic",
        llm_api_key="super-secret-api-key",
    )
    with TestClient(create_app(settings, llm_client=StubLLM())) as client:
        task = client.post(
            "/tasks",
            json=payload(),
            headers=authenticate("agent-secret-token"),
        ).json()
        reject_with = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "reject", "note": "凭据检查"},
            headers=authenticate("operator-secret-token"),
        )
        assert reject_with.status_code == 200

        response = client.get(
            "/audit/export", headers=authenticate("operator-secret-token")
        )

    assert response.status_code == 200
    raw = response.text
    for secret in (
        "agent-secret-token",
        "operator-secret-token",
        "admin-secret-token",
        "super-secret-api-key",
    ):
        assert secret not in raw
    assert "Authorization" not in raw
    assert "api_key" not in raw


def test_audit_export_reads_tasks_and_approvals_across_split_stores(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "orchestrator.db"),
        db_layout="split",
        inventory_db_path=str(tmp_path / "inventory.db"),
        mall_db_path=str(tmp_path / "mall.db"),
    )
    with TestClient(create_app(settings)) as client:
        task = client.post("/tasks", json=payload()).json()
        response = client.get("/audit/export")

    assert response.status_code == 200
    body = response.json()
    assert body["tasks"][0]["task_id"] == task["task_id"]
    assert body["approvals"][0]["approval_id"] == task["approval_id"]

    orchestrator = sqlite3.connect(settings.store_orchestrator)
    try:
        assert orchestrator.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        orchestrator.close()

    mall = sqlite3.connect(settings.store_mall)
    try:
        assert mall.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    finally:
        mall.close()
