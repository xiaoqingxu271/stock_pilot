"""FastAPI surface tests (submit / approve / resolve mapping)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import fetch_one
from fastapi.testclient import TestClient

from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.tools import StockPilotTools


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def payload(
    sku: str = "SKU-TSHIRT-001", spec: str = "白色-M", price: str = "59.00"
) -> dict:
    return {
        "sku_id": sku,
        "spec": spec,
        "target_price": price,
        "reason": "库存驱动调价",
    }


def as_decimal(value) -> Decimal:
    return Decimal(str(value))


def test_api_happy_path(client, settings: Settings):
    response = client.post("/tasks", json=payload())
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "awaiting_approval"
    assert task["listing_id"] == "LST-001"
    assert task["approval_id"]

    decision = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve", "note": "同意"},
    )
    assert decision.status_code == 200
    done = decision.json()
    assert done["status"] == "verified"
    assert as_decimal(done["final_price"]) == Decimal("59.00")
    assert done["verification"]["result"]["matched"] is True

    fetched = client.get(f"/tasks/{task['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "verified"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_api_insufficient_stock(client, settings: Settings):
    response = client.post(
        "/tasks", json=payload("SKU-HOODIE-003", "黑色-L", "99.00")
    )
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "aborted_no_stock"
    assert task["error_code"] == "INSUFFICIENT_STOCK"

    approvals = client.get("/approvals").json()
    assert approvals == []
    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-004'"
    )
    assert row["price_cents"] == 19900


def test_api_ambiguous_then_manual_mapping(client, settings: Settings):
    response = client.post(
        "/tasks", json=payload("SKU-AMBIG-002", "白色-M", "59.00")
    )
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "suspended_ambiguous"
    assert len(task["candidates"]) == 2
    assert task["approval_id"] is None

    # Nothing was written while suspended.
    assert client.get("/approvals").json() == []

    resolved = client.post(
        f"/tasks/{task['task_id']}/mapping", json={"listing_id": "LST-002"}
    )
    assert resolved.status_code == 200
    resumed = resolved.json()
    assert resumed["status"] == "awaiting_approval"
    assert resumed["listing_id"] == "LST-002"
    assert resumed["approval_id"]

    done = client.post(
        f"/approvals/{resumed['approval_id']}/decision",
        json={"decision": "approve"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "verified"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-002'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_api_rejection_rolls_back(client, settings: Settings):
    response = client.post("/tasks", json=payload())
    task = response.json()

    decision = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "reject", "note": "价格过低"},
    )
    assert decision.status_code == 200
    done = decision.json()
    assert done["status"] == "rolled_back"
    assert done["error_code"] == "APPROVAL_REJECTED"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"


def test_api_errors(client):
    assert (
        client.post(
            "/approvals/apr_missing/decision", json={"decision": "approve"}
        ).status_code
        == 404
    )

    task = client.post("/tasks", json=payload()).json()
    invalid_mapping = client.post(
        f"/tasks/{task['task_id']}/mapping", json={"listing_id": "LST-001"}
    )
    assert invalid_mapping.status_code == 409

    ambiguous = client.post(
        "/tasks", json=payload("SKU-AMBIG-002", "白色-M", "59.00")
    ).json()
    bad_choice = client.post(
        f"/tasks/{ambiguous['task_id']}/mapping", json={"listing_id": "LST-999"}
    )
    assert bad_choice.status_code == 400

    twice = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve"},
    )
    assert twice.status_code == 200
    again = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "reject"},
    )
    assert again.status_code == 409


def test_api_validation_rejects_negative_price(client):
    response = client.post("/tasks", json=payload(price="-1"))
    assert response.status_code == 422


def test_healthz_reports_readiness_and_queue_depth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["tool_client"] is True
    assert body["pending_approvals"] == 0
    assert body["recovery_issues"] == 0

    client.post("/tasks", json=payload())
    assert client.get("/healthz").json()["pending_approvals"] == 1


def test_healthz_reports_degraded_when_the_tool_client_is_down(settings: Settings):
    class Down:
        def __init__(self, inner: StockPilotTools) -> None:
            self._inner = inner
            self.settings = inner.settings

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def is_available(self) -> bool:
            return False

    inner = StockPilotTools(settings)
    app = create_app(settings, tool_client=Down(inner))
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["tool_client"] is False
        assert body["database"] is True
