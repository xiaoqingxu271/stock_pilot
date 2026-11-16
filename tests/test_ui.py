"""Approval UI shell and its enriched queue endpoint."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from stock_pilot.api import create_app
from stock_pilot.config import Settings


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def payload() -> dict:
    return {
        "sku_id": "SKU-TSHIRT-001",
        "spec": "白色-M",
        "target_price": "59.00",
        "reason": "库存驱动调价",
    }


def as_decimal(value) -> Decimal:
    return Decimal(str(value))


def test_ui_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "StockPilot" in response.text
    assert "/static/app.js" in response.text
    assert "/static/styles.css" in response.text


def test_ui_static_assets_are_served(client):
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "refresh" in js.text


def test_approval_queue_is_enriched_for_the_ui(client):
    task = client.post("/tasks", json=payload()).json()

    response = client.get("/approvals/queue")
    assert response.status_code == 200
    queue = response.json()
    assert len(queue) == 1

    item = queue[0]
    assert item["approval_id"] == task["approval_id"]
    assert item["task_id"] == task["task_id"]
    assert item["listing_id"] == "LST-001"
    assert item["sku_id"] == "SKU-TSHIRT-001"
    assert item["title"] == "经典白T恤"
    assert item["spec"] == "白色-M"
    assert as_decimal(item["current_price"]) == Decimal("79.00")
    assert as_decimal(item["original_price"]) == Decimal("79.00")
    assert as_decimal(item["new_price"]) == Decimal("59.00")
    assert item["proposal_reason"] == "库存驱动调价"
    assert item["status"] == "pending"

    decided = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve", "note": "同意"},
    )
    assert decided.status_code == 200
    assert client.get("/approvals/queue").json() == []


def test_approval_queue_filters_status(client):
    task = client.post("/tasks", json=payload()).json()
    client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "reject", "note": "价格过低"},
    )

    pending = client.get("/approvals/queue").json()
    assert pending == []

    rejected = client.get("/approvals/queue?status=rejected").json()
    assert len(rejected) == 1
    assert rejected[0]["decision_note"] == "价格过低"
