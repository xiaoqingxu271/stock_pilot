"""Integration tests for the real MCP stdio client boundary."""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal

import pytest
from conftest import count_rows, fetch_one
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot import approval
from stock_pilot.agent import build_graph, decide_and_resume, start_task
from stock_pilot.clients import MCPToolClient
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    PendingStage,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.models import (
    CheckInventoryInput,
    CreateTaskRequest,
    GetListingInput,
    PublishListingInput,
    UpdatePriceInput,
)


@pytest.fixture
def mcp_client(settings: Settings):
    client = MCPToolClient(settings, connect_timeout=30.0)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def memory_checkpointer():
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


def request(price: str = "59.00") -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id="SKU-TSHIRT-001",
        spec="白色-M",
        target_price=Decimal(price),
        reason="MCP 集成测试",
    )


def test_mcp_client_exposes_four_contract_tools(mcp_client: MCPToolClient):
    tools = {tool.name: tool for tool in mcp_client.list_tools()}
    assert set(tools) == {
        "check_inventory",
        "get_listing",
        "update_price",
        "publish_listing",
    }

    expected_inputs = {
        "check_inventory": {"sku_id", "spec"},
        "get_listing": {"sku_id"},
        "update_price": {"listing_id", "new_price", "proposal_reason"},
        "publish_listing": {"listing_id", "approval_id"},
    }
    for name, expected in expected_inputs.items():
        schema = tools[name].input_schema
        assert set(schema["properties"]) == expected
        assert tools[name].output_schema is not None


def test_mcp_client_full_tool_chain(mcp_client: MCPToolClient, settings: Settings):
    inventory = mcp_client.check_inventory(
        CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
    )
    assert inventory.ok
    assert inventory.quantity == 20

    resolved = mcp_client.get_listing(GetListingInput(sku_id="SKU-TSHIRT-001"))
    assert resolved.result is ListingResult.RESOLVED
    assert resolved.listing is not None
    assert resolved.listing.price == Decimal("79.00")

    staged = mcp_client.update_price(
        UpdatePriceInput(
            listing_id="LST-001",
            new_price=Decimal("59.00"),
            proposal_reason="MCP 集成测试",
        )
    )
    assert staged.ok
    assert staged.approval_id

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    pending = mcp_client.get_listing(GetListingInput(sku_id="SKU-TSHIRT-001"))
    assert pending.pending is not None
    assert pending.pending.stage is PendingStage.PRICE_STAGED
    assert pending.pending.staged_price == Decimal("59.00")

    refused = mcp_client.publish_listing(
        PublishListingInput(listing_id="LST-001", approval_id=staged.approval_id)
    )
    assert not refused.ok
    assert refused.error_code is ErrorCode.APPROVAL_NOT_APPROVED

    decided = mcp_client.decide_approval(
        staged.approval_id, ApprovalDecision.APPROVE, "同意"
    )
    assert decided.status is ApprovalStatus.APPROVED

    published = mcp_client.publish_listing(
        PublishListingInput(listing_id="LST-001", approval_id=staged.approval_id)
    )
    assert published.ok
    assert published.final_status is ListingStatus.PUBLISHED
    assert published.final_price == Decimal("59.00")

    final = mcp_client.get_listing(GetListingInput(sku_id="SKU-TSHIRT-001"))
    assert final.listing is not None
    assert final.listing.price == Decimal("59.00")
    assert final.listing.status is ListingStatus.PUBLISHED
    assert final.pending is None


def test_graph_runs_through_mcp_client(
    mcp_client: MCPToolClient, memory_checkpointer
):
    graph = build_graph(mcp_client, memory_checkpointer)
    task = start_task(mcp_client, graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert task.listing_id == "LST-001"

    done = decide_and_resume(
        mcp_client, graph, task.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.VERIFIED
    assert done.verification is not None
    assert done.verification.result.matched


def test_api_runs_through_mcp_client(
    mcp_client: MCPToolClient, settings: Settings
):
    from stock_pilot.api import create_app

    app = create_app(settings, tool_client=mcp_client)
    with TestClient(app) as client:
        created = client.post(
            "/tasks",
            json={
                "sku_id": "SKU-TSHIRT-001",
                "spec": "白色-M",
                "target_price": "59.00",
                "reason": "MCP API 集成测试",
            },
        )
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == "awaiting_approval"

        decided = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve", "note": "同意"},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "verified"

        events = client.get(f"/tasks/{task['task_id']}/events").json()
        assert [event["tool"] for event in events] == [
            "check_inventory",
            "get_listing",
            "update_price",
            "publish_listing",
            "get_listing",
        ]
        assert all(event["outcome"] == "ok" for event in events)

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_unavailable_mcp_server_fails_safe(
    settings: Settings, memory_checkpointer
):
    broken = MCPToolClient(
        settings,
        command=sys.executable,
        args=["-c", "import sys; sys.exit(1)"],
        connect_timeout=10.0,
    )
    try:
        inventory = broken.check_inventory(
            CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
        )
        assert not inventory.ok
        assert inventory.error_code is ErrorCode.TOOL_UNAVAILABLE

        graph = build_graph(broken, memory_checkpointer)
        task = start_task(broken, graph, request())
        assert task.status is TaskStatus.ABORTED_UNKNOWN_STATE
        # The first probe exhausts read retries; the graph call then finds the
        # circuit open. Either way the task must stop before any write.
        assert task.error_code in {
            ErrorCode.TOOL_UNAVAILABLE,
            ErrorCode.CIRCUIT_OPEN,
        }
        assert count_rows(settings, "staging") == 0
        assert count_rows(settings, "approvals") == 0
    finally:
        broken.close()


def test_mcp_server_lost_after_staging_stops_before_publish(
    mcp_client: MCPToolClient, memory_checkpointer, settings: Settings
):
    graph = build_graph(mcp_client, memory_checkpointer)
    task = start_task(mcp_client, graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL

    # Simulate the MCP server dying before the approval resume.
    mcp_client.close()

    done = decide_and_resume(
        mcp_client, graph, task.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.ABORTED_UNKNOWN_STATE
    assert done.error_code in {
        ErrorCode.TOOL_UNAVAILABLE,
        ErrorCode.TOOL_TIMEOUT,
        ErrorCode.CIRCUIT_OPEN,
    }

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    staging = approval.get_staging_for_approval(settings.db_path, task.approval_id)
    assert staging is not None
    assert staging.status is StagingStatus.PRICE_STAGED
