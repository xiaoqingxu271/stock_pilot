"""Transport reconnection keeps failures safe: no write is ever replayed.

The client re-dials a restarted HTTP MCP server with exponential backoff.
A task interrupted while the mall was down ends in a non-terminal-safe
``ABORTED_UNKNOWN_STATE``; a fresh task after recovery completes normally and
there is exactly one successful ``publish_listing`` in the audit trail.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal

from conftest import fetch_one
from helpers import ServerHandle, free_port, wait_until
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot.agent import build_graph, decide_and_resume, start_task
from stock_pilot.clients import SplitMCPToolClient
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalDecision,
    ErrorCode,
    TaskStatus,
)
from stock_pilot.mcp_http import create_http_app
from stock_pilot.mcp_server import build_inventory_server, build_mall_server
from stock_pilot.models import CheckInventoryInput, CreateTaskRequest, GetListingInput
from stock_pilot.tools import StockPilotTools

TOKEN = "test-agent-token"


def _request(price: str) -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id="SKU-TSHIRT-001",
        spec="白色-M",
        target_price=Decimal(price),
        reason="重连测试",
    )


def test_http_client_recovers_and_never_replays_a_write(settings: Settings):
    inventory_port = free_port()
    mall_port = free_port()
    inventory = ServerHandle(
        create_http_app(
            build_inventory_server(StockPilotTools(settings)), token=TOKEN
        ),
        inventory_port,
    )
    mall = ServerHandle(
        create_http_app(build_mall_server(StockPilotTools(settings)), token=TOKEN),
        mall_port,
    )
    inventory.start()
    mall.start()

    inventory_url = f"http://127.0.0.1:{inventory_port}/mcp"
    mall_url = f"http://127.0.0.1:{mall_port}/mcp"
    mcp_settings = dataclasses.replace(
        settings,
        tool_timeout_seconds=5.0,
        inventory_mcp_url=inventory_url,
        mall_mcp_url=mall_url,
        agent_token=TOKEN,
    )
    client = SplitMCPToolClient(
        mcp_settings,
        inventory_url=inventory_url,
        mall_url=mall_url,
        headers={"Authorization": f"Bearer {TOKEN}"},
        connect_timeout=30.0,
    )
    graph = build_graph(
        client, SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    )
    restarted_mall: ServerHandle | None = None
    try:
        task_a = start_task(client, graph, _request("59.00"))
        assert task_a.status is TaskStatus.AWAITING_APPROVAL
        assert client.is_available()

        # Take the mall down. Approval is recorded locally, then publish fails
        # safely; the single read-only reconciliation also fails, so the task
        # stops as "unknown state" instead of retrying the write.
        mall.stop()
        assert wait_until(lambda: not client.is_available(), timeout=20)

        failed = client.get_listing(GetListingInput(sku_id="SKU-TSHIRT-001"))
        assert not failed.ok
        assert failed.error_code in {
            ErrorCode.TOOL_UNAVAILABLE,
            ErrorCode.CIRCUIT_OPEN,
        }

        aborted = decide_and_resume(
            client, graph, task_a.approval_id, ApprovalDecision.APPROVE
        )
        assert aborted.status is TaskStatus.ABORTED_UNKNOWN_STATE

        # A fresh proposal is blocked by the interrupted one; clear it the way
        # an operator would (staging belongs to the aborted task).
        client.abandon_approval(task_a.approval_id, "测试清理中断提案")

        # Bring the mall back on the same port; the client reconnects on its
        # own without any manual intervention.
        restarted_mall = ServerHandle(
            create_http_app(
                build_mall_server(StockPilotTools(settings)), token=TOKEN
            ),
            mall_port,
        )
        restarted_mall.start()
        # Recovery means transport up *and* breakers no longer rejecting calls;
        # an open breaker from the outage closes after its cooldown.
        assert wait_until(
            lambda: client.is_available()
            and not any(client.circuit_states().values()),
            timeout=30,
        )

        inventory_read = client.check_inventory(
            CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
        )
        assert inventory_read.ok

        task_b = start_task(client, graph, _request("49.00"))
        assert task_b.status is TaskStatus.AWAITING_APPROVAL, task_b.error_code
        done = decide_and_resume(
            client, graph, task_b.approval_id, ApprovalDecision.APPROVE
        )
        assert done.status is TaskStatus.VERIFIED
        assert done.final_price == Decimal("49.00")
    finally:
        client.close()
        if restarted_mall is not None:
            restarted_mall.stop()
        mall.stop()
        inventory.stop()

    listing = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert listing["price_cents"] == 4900
    assert listing["status"] == "published"

    conn = sqlite3.connect(settings.db_path)
    try:
        published = conn.execute(
            "SELECT COUNT(*) FROM tool_events"
            " WHERE tool = 'publish_listing' AND outcome = 'ok'"
            " AND task_id IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert published == 1, "the interrupted write must never be replayed"
