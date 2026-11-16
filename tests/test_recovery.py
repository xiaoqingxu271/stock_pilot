"""Restart recovery, idempotency, operator identity, and audit trail."""

from __future__ import annotations

import sqlite3

from conftest import count_rows, fetch_one
from fastapi.testclient import TestClient

from stock_pilot import approval
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.enums import ApprovalDecision, StagingStatus
from stock_pilot.models import ApprovalDecisionRequest


def payload(
    sku: str = "SKU-TSHIRT-001", spec: str = "白色-M", price: str = "59.00"
) -> dict:
    return {
        "sku_id": sku,
        "spec": spec,
        "target_price": price,
        "reason": "库存驱动调价",
    }


def new_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def submit(client: TestClient, **overrides) -> dict:
    body = payload(**overrides)
    response = client.post("/tasks", json=body)
    assert response.status_code == 201
    return response.json()


def test_restart_resumes_pending_approval(settings: Settings):
    with new_client(settings) as first:
        task = submit(first)

    # A fresh process with a fresh graph/checkpointer reads the same DB.
    with new_client(settings) as second:
        decided = second.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve", "note": "重启后审批", "decided_by": "张三"},
        )
        assert decided.status_code == 200
        done = decided.json()
        assert done["status"] == "verified"
        assert done["verification"]["result"]["matched"] is True

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_restart_resumes_mapping_confirmation(settings: Settings):
    with new_client(settings) as first:
        task = first.post(
            "/tasks", json=payload("SKU-AMBIG-002", "白色-M", "59.00")
        ).json()
        assert task["status"] == "suspended_ambiguous"

    with new_client(settings) as second:
        mapped = second.post(
            f"/tasks/{task['task_id']}/mapping", json={"listing_id": "LST-002"}
        )
        assert mapped.status_code == 200
        assert mapped.json()["status"] == "awaiting_approval"
        done = second.post(
            f"/approvals/{mapped.json()['approval_id']}/decision",
            json={"decision": "approve"},
        )
        assert done.json()["status"] == "verified"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-002'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_recovery_reports_approved_but_not_published(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)

        # Simulate a crash after the human decision but before graph resume.
        approval.decide_approval(
            settings.db_path,
            task["approval_id"],
            ApprovalDecision.APPROVE,
            "同意",
            "李四",
        )

        issues = client.get("/recovery").json()
        assert len(issues) == 1
        issue = issues[0]
        assert issue["kind"] == "approved_not_published"
        assert issue["task_id"] == task["task_id"]
        assert issue["approval_status"] == "approved"
        assert issue["staging_status"] == "price_staged"
        assert issue["listing_id"] == "LST-001"

        # Nothing was published just because the decision exists.
        row = fetch_one(
            settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
        )
        assert row["price_cents"] == 7900

        recovered = client.post(f"/tasks/{task['task_id']}/recover")
        assert recovered.status_code == 200
        done = recovered.json()
        assert done["status"] == "verified"
        assert client.get("/recovery").json() == []

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_recovery_reports_rejected_but_not_closed(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        approval.decide_approval(
            settings.db_path,
            task["approval_id"],
            ApprovalDecision.REJECT,
            "价格过低",
            "王五",
        )

        issues = client.get("/recovery").json()
        assert len(issues) == 1
        assert issues[0]["kind"] == "rejected_not_closed"
        assert issues[0]["staging_status"] == "discarded"

        recovered = client.post(f"/tasks/{task['task_id']}/recover")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "rolled_back"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    staging = approval.get_staging_for_approval(settings.db_path, task["approval_id"])
    assert staging is not None
    assert staging.status is StagingStatus.DISCARDED


def test_recovery_is_empty_for_a_normal_pending_approval(settings: Settings):
    with new_client(settings) as client:
        submit(client)
        assert client.get("/recovery").json() == []


def test_recover_rejects_a_pending_approval(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        response = client.post(f"/tasks/{task['task_id']}/recover")
        assert response.status_code == 409


def test_idempotent_task_submission(settings: Settings):
    headers = {"Idempotency-Key": "submit-001"}
    with new_client(settings) as client:
        first = client.post("/tasks", json=payload(), headers=headers)
        assert first.status_code == 201

        second = client.post("/tasks", json=payload(), headers=headers)
        assert second.status_code == 200
        assert second.json()["task_id"] == first.json()["task_id"]
        assert count_rows(settings, "tasks") == 1
        assert count_rows(settings, "approvals") == 1


def test_idempotency_key_conflicts_with_a_different_request(settings: Settings):
    headers = {"Idempotency-Key": "submit-002"}
    with new_client(settings) as client:
        client.post("/tasks", json=payload(), headers=headers)
        conflict = client.post(
            "/tasks", json=payload(price="49.00"), headers=headers
        )
        assert conflict.status_code == 409
        assert count_rows(settings, "tasks") == 1


def test_blank_idempotency_key_is_rejected(settings: Settings):
    with new_client(settings) as client:
        response = client.post(
            "/tasks", json=payload(), headers={"Idempotency-Key": "   "}
        )
        assert response.status_code == 400
        assert count_rows(settings, "tasks") == 0


def test_repeating_the_same_decision_is_idempotent(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        first = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve", "decided_by": "赵六"},
        )
        assert first.status_code == 200
        assert first.json()["status"] == "verified"

        replay = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve", "decided_by": "赵六"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "verified"
        assert replay.json()["task_id"] == task["task_id"]

        opposite = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "reject", "note": "改主意"},
        )
        assert opposite.status_code == 409

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_decision_records_the_operator(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "reject", "note": "价格过低", "decided_by": "王五"},
        )

        record = client.get(f"/approvals/{task['approval_id']}").json()
        assert record["decided_by"] == "王五"
        assert record["decision_note"] == "价格过低"


def test_tool_events_form_a_task_timeline(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve"},
        )

        response = client.get(f"/tasks/{task['task_id']}/events")
        assert response.status_code == 200
        events = response.json()

    assert [event["tool"] for event in events] == [
        "check_inventory",
        "get_listing",
        "update_price",
        "check_inventory",
        "publish_listing",
        "get_listing",
    ]
    assert all(event["task_id"] == task["task_id"] for event in events)
    assert all(event["outcome"] == "ok" for event in events)
    assert all(event["attempt"] == 1 for event in events)
    assert all(event["latency_ms"] >= 0 for event in events)


def test_tool_events_404_for_unknown_task(settings: Settings):
    with new_client(settings) as client:
        assert client.get("/tasks/task_missing/events").status_code == 404


def test_publish_recheck_abandons_when_stock_depleted(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)

        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE inventory_records SET quantity = 0 WHERE id = 'INV-001'"
            )

        decided = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve", "note": "同意"},
        )
        assert decided.status_code == 200
        done = decided.json()
        assert done["status"] == "rolled_back"
        assert done["error_code"] == "INSUFFICIENT_STOCK"

        record = client.get(f"/approvals/{task['approval_id']}").json()
        assert record["status"] == "abandoned"
        assert "发布前库存" in record["abandon_reason"]
        assert record["abandoned_at"] is not None

        events = client.get(f"/tasks/{task['task_id']}/events").json()
        assert [event["tool"] for event in events] == [
            "check_inventory",
            "get_listing",
            "update_price",
            "check_inventory",
        ]
        assert events[-1]["outcome"] == "ok"
        assert client.get("/recovery").json() == []

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    staging = approval.get_staging_for_approval(settings.db_path, task["approval_id"])
    assert staging is not None
    assert staging.status is StagingStatus.DISCARDED


def test_recovery_reports_abandoned_but_not_closed(settings: Settings):
    with new_client(settings) as client:
        task = submit(client)
        approval.decide_approval(
            settings.db_path,
            task["approval_id"],
            ApprovalDecision.APPROVE,
            "同意",
            "李四",
        )
        approval.abandon_approval(
            settings.db_path, task["approval_id"], "发布前库存复核不通过"
        )

        issues = client.get("/recovery").json()
        assert len(issues) == 1
        assert issues[0]["kind"] == "abandoned_not_closed"
        assert issues[0]["approval_status"] == "abandoned"
        assert issues[0]["staging_status"] == "discarded"

        recovered = client.post(f"/tasks/{task['task_id']}/recover")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "rolled_back"
        assert recovered.json()["error_code"] == "INSUFFICIENT_STOCK"

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"


def test_decision_request_model_exposes_operator():
    request = ApprovalDecisionRequest(
        decision=ApprovalDecision.APPROVE, decided_by="张三"
    )
    assert request.decided_by == "张三"
    assert request.note is None
