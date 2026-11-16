"""Split stores: inventory / mall(+approvals) / orchestrator are separate files.

The publish saga is: recheck stock -> reserve -> mall commit -> release. A lost
publish response is resolved read-only by adopting the committed listing, so a
committed write is never repeated.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from helpers import ServerHandle, free_port
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot.agent import build_graph, decide_and_resume, start_task
from stock_pilot.api import create_app
from stock_pilot.clients import SplitMCPToolClient
from stock_pilot.config import Settings
from stock_pilot.enums import ApprovalDecision, ErrorCode, ListingStatus, TaskStatus
from stock_pilot.mcp_http import create_http_app
from stock_pilot.mcp_server import build_inventory_server, build_mall_server
from stock_pilot.models import (
    CheckInventoryInput,
    CreateTaskRequest,
    GetListingInput,
    PublishListingOutput,
    ReleaseInventoryInput,
    ReserveInventoryInput,
)
from stock_pilot.tools import StockPilotTools


@pytest.fixture
def split_settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "orchestrator.db"),
        db_layout="split",
        tool_timeout_seconds=2.0,
        read_max_retries=1,
        circuit_failure_threshold=3,
        circuit_cooldown_seconds=1.0,
    )


@pytest.fixture
def split_tools(split_settings: Settings) -> StockPilotTools:
    return StockPilotTools(split_settings)


@pytest.fixture
def split_graph(split_tools: StockPilotTools):
    return build_graph(
        split_tools,
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )


def request(price: str = "59.00") -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id="SKU-TSHIRT-001",
        spec="白色-M",
        target_price=Decimal(price),
        reason="拆分存储测试",
    )


def table_names(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def scalar(path: str, sql: str, params: tuple = ()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


class CommitThenLoseResponse:
    """Mall commits, the response never reaches the orchestrator."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.settings = inner.settings
        self.publish_calls = 0

    def publish_listing(self, request):  # noqa: ANN001 - test double
        self.publish_calls += 1
        result = self.inner.publish_listing(request)
        assert result.ok
        return PublishListingOutput.fail(
            ErrorCode.TOOL_UNAVAILABLE, "simulated lost response"
        )

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class FailBeforeCommit:
    """Transport fails before the mall sees the write."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.settings = inner.settings
        self.publish_calls = 0

    def publish_listing(self, request):  # noqa: ANN001 - test double
        self.publish_calls += 1
        return PublishListingOutput.fail(
            ErrorCode.TOOL_UNAVAILABLE, "simulated transport failure"
        )

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def test_each_store_owns_only_its_tables(split_settings: Settings):
    StockPilotTools(split_settings)

    assert table_names(split_settings.store_orchestrator) == {
        "tasks",
        "tool_events",
        "outbox_events",
        "sqlite_sequence",
    }
    assert table_names(split_settings.store_inventory) == {"inventory_records"}
    assert table_names(split_settings.store_mall) == {
        "listings",
        "approvals",
        "staging",
    }
    assert split_settings.approval_db == split_settings.store_mall


def test_reservation_is_exclusive_idempotent_and_releasable(
    split_tools: StockPilotTools,
):
    first = split_tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id="apr-a"
        )
    )
    assert first.ok and first.reserved

    again = split_tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id="apr-a"
        )
    )
    assert again.ok and again.reserved

    blocked = split_tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id="apr-b"
        )
    )
    assert not blocked.ok
    assert blocked.error_code is ErrorCode.STOCK_RESERVED

    released = split_tools.release_inventory(
        ReleaseInventoryInput(approval_id="apr-a")
    )
    assert released.ok and released.released

    second = split_tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id="apr-b"
        )
    )
    assert second.ok and second.reserved


def test_split_happy_path_commits_and_releases_reservation(
    split_settings: Settings, split_tools: StockPilotTools, split_graph
):
    task = start_task(split_tools, split_graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL

    done = decide_and_resume(
        split_tools, split_graph, task.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.VERIFIED
    assert done.final_price == Decimal("59.00")

    assert (
        scalar(
            split_settings.store_mall,
            "SELECT price_cents FROM listings WHERE id = 'LST-001'",
        )
        == 5900
    )
    assert (
        scalar(
            split_settings.store_mall,
            "SELECT status FROM approvals WHERE id = ?",
            (task.approval_id,),
        )
        == "consumed"
    )
    assert (
        scalar(
            split_settings.store_inventory,
            "SELECT reserved_by FROM inventory_records WHERE id = 'INV-001'",
        )
        is None
    )
    assert (
        scalar(
            split_settings.store_orchestrator,
            "SELECT status FROM tasks WHERE id = ?",
            (task.task_id,),
        )
        == "verified"
    )


def test_mall_committed_but_response_lost_is_adopted_once(
    split_settings: Settings, split_tools: StockPilotTools
):
    client = CommitThenLoseResponse(split_tools)
    graph = build_graph(
        client,
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )

    task = start_task(client, graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL

    done = decide_and_resume(
        client, graph, task.approval_id, ApprovalDecision.APPROVE
    )

    # The uncertain publish was resolved by reading the mall: the committed
    # listing is adopted, verified, and never re-published.
    assert done.status is TaskStatus.VERIFIED
    assert client.publish_calls == 1
    assert done.verification is not None
    assert done.verification.result.matched
    assert (
        scalar(
            split_settings.store_mall,
            "SELECT status FROM approvals WHERE id = ?",
            (task.approval_id,),
        )
        == "consumed"
    )
    assert (
        scalar(
            split_settings.store_inventory,
            "SELECT reserved_by FROM inventory_records WHERE id = 'INV-001'",
        )
        is None
    )


def test_publish_failure_before_commit_stops_and_releases(
    split_settings: Settings, split_tools: StockPilotTools
):
    client = FailBeforeCommit(split_tools)
    graph = build_graph(
        client,
        SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)),
    )

    task = start_task(client, graph, request())
    done = decide_and_resume(
        client, graph, task.approval_id, ApprovalDecision.APPROVE
    )

    assert done.status is TaskStatus.ABORTED_UNKNOWN_STATE
    assert client.publish_calls == 1
    assert (
        scalar(
            split_settings.store_mall,
            "SELECT price_cents FROM listings WHERE id = 'LST-001'",
        )
        == 7900
    )
    assert (
        scalar(
            split_settings.store_mall,
            "SELECT status FROM listings WHERE id = 'LST-001'",
        )
        == ListingStatus.DRAFT.value
    )
    assert (
        scalar(
            split_settings.store_inventory,
            "SELECT reserved_by FROM inventory_records WHERE id = 'INV-001'",
        )
        is None
    )
    # The uncommitted proposal remains visible to recovery/manual cleanup.
    assert (
        scalar(
            split_settings.store_mall,
            "SELECT status FROM staging WHERE approval_id = ?",
            (task.approval_id,),
        )
        == "price_staged"
    )


def test_split_http_servers_route_the_reservation_saga(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "orchestrator.db"),
        db_layout="split",
        tool_timeout_seconds=15.0,
    )
    inventory_port = free_port()
    mall_port = free_port()
    inventory = ServerHandle(
        create_http_app(
            build_inventory_server(StockPilotTools(settings)), token=None
        ),
        inventory_port,
    )
    mall = ServerHandle(
        create_http_app(build_mall_server(StockPilotTools(settings)), token=None),
        mall_port,
    )
    inventory.start()
    mall.start()
    client = SplitMCPToolClient(
        settings,
        inventory_url=f"http://127.0.0.1:{inventory_port}/mcp",
        mall_url=f"http://127.0.0.1:{mall_port}/mcp",
        connect_timeout=30.0,
    )
    try:
        stock = client.check_inventory(
            CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
        )
        assert stock.ok and stock.quantity == 20

        listing = client.get_listing(GetListingInput(sku_id="SKU-TSHIRT-001"))
        assert listing.ok

        reserved = client.reserve_inventory(
            ReserveInventoryInput(
                inventory_record_id="INV-001", approval_id="apr-split"
            )
        )
        assert reserved.ok and reserved.reserved

        listed = client.list_reservations()
        assert listed.ok
        assert [item.approval_id for item in listed.reservations] == ["apr-split"]

        released = client.release_inventory(
            ReleaseInventoryInput(approval_id="apr-split")
        )
        assert released.ok and released.released
    finally:
        client.close()
        mall.stop()
        inventory.stop()


def test_split_api_and_metrics_read_across_stores(split_settings: Settings):
    with TestClient(create_app(split_settings)) as client:
        created = client.post(
            "/tasks",
            json={
                "sku_id": "SKU-TSHIRT-001",
                "spec": "白色-M",
                "target_price": "59.00",
                "reason": "拆分 API 测试",
            },
        )
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == "awaiting_approval"

        approvals = client.get("/approvals").json()
        assert len(approvals) == 1
        assert approvals[0]["approval_id"] == task["approval_id"]

        decision = client.post(
            f"/approvals/{task['approval_id']}/decision",
            json={"decision": "approve"},
        )
        assert decision.status_code == 200
        assert decision.json()["status"] == "verified"

        assert client.get("/healthz").json()["status"] == "ok"
        metrics = client.get("/metrics").text
        assert 'stock_pilot_tasks_total{status="verified"} 1.0' in metrics
        assert "stock_pilot_approvals_pending 0.0" in metrics
