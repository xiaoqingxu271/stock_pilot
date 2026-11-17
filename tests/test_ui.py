"""Approval UI shell and its enriched queue endpoint."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from stock_pilot.api import create_app
from stock_pilot.config import Settings


class StubLLM:
    """Deterministic intake client for console tests; no network."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {
            "sku_id": "SKU-TSHIRT-001",
            "spec": "白色-M",
            "target_price": 59,
            "reason": "库存驱动调价",
        }
        self.calls: list[str] = []

    def extract(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return dict(self.result)


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
    assert 'id="operator-name"' in response.text
    assert 'id="admin-token"' in response.text
    assert 'id="recovery-panel"' in response.text
    assert 'id="intake-panel"' in response.text
    assert 'id="intake-text"' in response.text
    assert 'id="intake-draft-btn"' in response.text
    assert 'id="intake-confirm-btn"' in response.text


def test_ui_static_assets_are_served(client):
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "refresh" in js.text
    assert "/recovery" in js.text
    assert "decided_by" in js.text
    assert "admin-token" in js.text
    assert "Authorization" in js.text
    assert "/abandon" in js.text
    assert "staging_orphaned" in js.text
    assert "published_verify_pending" in js.text
    assert "/capabilities" in js.text
    assert "/tasks/intake" in js.text
    assert "intake-confirm-btn" in js.text


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


def test_recovery_endpoint_is_served(client):
    assert client.get("/recovery").json() == []
    task = client.post("/tasks", json=payload()).json()
    assert client.get(f"/tasks/{task['task_id']}/events").status_code == 200


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


def test_capabilities_report_intake_disabled(client):
    response = client.get("/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "intake_enabled": False,
        "llm_model": None,
        "db_layout": "shared",
        "auth_enabled": False,
    }


def test_capabilities_report_enabled_intake_without_the_api_key(settings: Settings):
    fake = StubLLM()
    with TestClient(create_app(settings, llm_client=fake)) as test_client:
        caps = test_client.get("/capabilities").json()

    assert caps["intake_enabled"] is True
    assert caps["llm_model"] == settings.llm_model
    assert "api_key" not in caps
    assert fake.calls == []


def test_capabilities_report_split_layout_and_auth(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "pilot.db"),
        db_layout="split",
        agent_token="agent-token",
    )
    with TestClient(create_app(settings)) as test_client:
        caps = test_client.get("/capabilities").json()

    assert caps["db_layout"] == "split"
    assert caps["auth_enabled"] is True


def test_intake_console_draft_is_inert_until_confirmed(settings: Settings):
    fake = StubLLM()
    with TestClient(create_app(settings, llm_client=fake)) as test_client:
        draft = test_client.post(
            "/tasks/intake",
            json={"text": "把 SKU-TSHIRT-001 白色-M 调到 59 元，理由清库存"},
        )
        assert draft.status_code == 200
        # Generating a draft must not create a task, staging row, or approval.
        assert test_client.get("/tasks").json() == []
        assert test_client.get("/approvals").json() == []

        # The console forwards only these four fields on confirmation.
        confirmed = test_client.post(
            "/tasks",
            json={
                key: draft.json()[key]
                for key in ("sku_id", "spec", "target_price", "reason")
            },
        )

    assert confirmed.status_code == 201
    assert confirmed.json()["status"] == "awaiting_approval"
    assert fake.calls == ["把 SKU-TSHIRT-001 白色-M 调到 59 元，理由清库存"]
