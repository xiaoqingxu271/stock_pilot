"""Static bearer-token roles on the FastAPI surface."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient

from stock_pilot.api import create_app
from stock_pilot.config import Settings

AGENT = {"Authorization": "Bearer agent-token"}
OPERATOR = {"Authorization": "Bearer operator-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
WRONG = {"Authorization": "Bearer wrong-token"}


def payload() -> dict:
    return {
        "sku_id": "SKU-TSHIRT-001",
        "spec": "白色-M",
        "target_price": "59.00",
        "reason": "鉴权测试",
    }


@pytest.fixture
def secured_settings(settings: Settings) -> Settings:
    return dataclasses.replace(
        settings,
        agent_token="agent-token",
        operator_token="operator-token",
        admin_token="admin-token",
    )


@pytest.fixture
def secured(secured_settings: Settings):
    with TestClient(create_app(secured_settings)) as client:
        yield client


def create_task(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post("/tasks", json=payload(), headers=headers)
    assert response.status_code == 201
    return response.json()


def test_missing_or_wrong_token_is_rejected(secured):
    assert secured.post("/tasks", json=payload()).status_code == 401
    assert (
        secured.post("/tasks", json=payload(), headers=WRONG).status_code == 401
    )
    # Read-only endpoints stay open for the demo console.
    assert secured.get("/tasks").status_code == 200
    assert secured.get("/healthz").status_code == 200


def test_agent_can_submit_but_not_decide(secured):
    task = create_task(secured, AGENT)
    assert task["submitted_by"] == "agent"

    forbidden = secured.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve"},
        headers=AGENT,
    )
    assert forbidden.status_code == 403

    # An operator cannot submit repricing tasks.
    assert (
        secured.post("/tasks", json=payload(), headers=OPERATOR).status_code == 403
    )


def test_operator_identity_overrides_the_request_body(secured):
    task = create_task(secured, AGENT)
    queue = secured.get("/approvals/queue").json()
    assert queue[0]["submitted_by"] == "agent"

    decided = secured.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve", "decided_by": "冒充的操作人"},
        headers=OPERATOR,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "verified"

    record = secured.get(f"/approvals/{task['approval_id']}").json()
    assert record["decided_by"] == "operator"


def test_admin_token_covers_agent_and_operator_actions(secured):
    task = create_task(secured, ADMIN)
    assert task["submitted_by"] == "admin"
    decided = secured.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve"},
        headers=ADMIN,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "verified"


def test_abandon_requires_operator_and_records_identity(secured):
    task = create_task(secured, AGENT)
    # Simulate an approved proposal whose task died before publish.
    with sqlite3.connect(secured.app.state.settings.db_path) as conn:
        conn.execute(
            "UPDATE approvals SET status = 'approved' WHERE id = ?",
            (task["approval_id"],),
        )
        conn.execute(
            "UPDATE tasks SET status = 'aborted_unknown_state' WHERE id = ?",
            (task["task_id"],),
        )

    assert (
        secured.post(
            f"/approvals/{task['approval_id']}/abandon", json={}
        ).status_code
        == 401
    )
    assert (
        secured.post(
            f"/approvals/{task['approval_id']}/abandon", json={}, headers=AGENT
        ).status_code
        == 403
    )

    withdrawn = secured.post(
        f"/approvals/{task['approval_id']}/abandon",
        json={"reason": "人工清理"},
        headers=OPERATOR,
    )
    assert withdrawn.status_code == 200
    record = withdrawn.json()
    assert record["status"] == "abandoned"
    assert record["abandoned_by"] == "operator"
