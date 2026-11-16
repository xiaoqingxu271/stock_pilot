"""Split inventory/mall MCP servers over Streamable HTTP, with bearer auth."""

from __future__ import annotations

import dataclasses
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal

import pytest
import uvicorn
from conftest import fetch_one
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot.agent import build_graph, decide_and_resume, start_task
from stock_pilot.clients import SplitMCPToolClient
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalDecision,
    ErrorCode,
    ListingResult,
    TaskStatus,
)
from stock_pilot.mcp_http import create_http_app
from stock_pilot.mcp_server import build_inventory_server, build_mall_server
from stock_pilot.models import (
    CheckInventoryInput,
    CreateTaskRequest,
    GetListingInput,
)
from stock_pilot.tools import StockPilotTools

TOKEN = "test-agent-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerHandle:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            # A client may still hold an MCP stream open; don't wait forever.
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("HTTP MCP server thread died during startup")
            time.sleep(0.02)
        raise RuntimeError("HTTP MCP server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def http_servers(settings: Settings):
    inventory_port = free_port()
    mall_port = free_port()
    inventory = ServerHandle(
        create_http_app(
            build_inventory_server(StockPilotTools(settings)),
            token=TOKEN,
        ),
        inventory_port,
    )
    mall = ServerHandle(
        create_http_app(
            build_mall_server(StockPilotTools(settings)), token=TOKEN
        ),
        mall_port,
    )
    inventory.start()
    mall.start()
    try:
        yield {
            "inventory": f"http://127.0.0.1:{inventory_port}/mcp",
            "mall": f"http://127.0.0.1:{mall_port}/mcp",
        }
    finally:
        mall.stop()
        inventory.stop()


def http_settings(settings: Settings, urls: dict[str, str]) -> Settings:
    return dataclasses.replace(
        settings,
        tool_timeout_seconds=15.0,
        inventory_mcp_url=urls["inventory"],
        mall_mcp_url=urls["mall"],
        agent_token=TOKEN,
    )


def request(price: str = "59.00") -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id="SKU-TSHIRT-001",
        spec="白色-M",
        target_price=Decimal(price),
        reason="HTTP 拆分集成测试",
    )


def test_split_servers_route_each_tool(http_servers, settings: Settings):
    client = SplitMCPToolClient(
        http_settings(settings, http_servers),
        inventory_url=http_servers["inventory"],
        mall_url=http_servers["mall"],
        headers={"Authorization": f"Bearer {TOKEN}"},
        connect_timeout=30.0,
    )
    try:
        inventory = client.check_inventory(
            CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
        )
        assert inventory.ok
        assert inventory.quantity == 20

        listing = client.get_listing(
            GetListingInput(sku_id="SKU-TSHIRT-001")
        )
        assert listing.result is ListingResult.RESOLVED
        assert listing.listing is not None
        assert listing.listing.listing_id == "LST-001"
        assert client.is_available()
    finally:
        client.close()


def test_full_graph_over_split_servers(http_servers, settings: Settings):
    mcp_settings = http_settings(settings, http_servers)
    client = SplitMCPToolClient(
        mcp_settings,
        inventory_url=http_servers["inventory"],
        mall_url=http_servers["mall"],
        headers={"Authorization": f"Bearer {TOKEN}"},
        connect_timeout=30.0,
    )
    graph = build_graph(
        client, SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    )
    try:
        task = start_task(client, graph, request())
        assert task.status is TaskStatus.AWAITING_APPROVAL
        assert task.listing_id == "LST-001"

        done = decide_and_resume(
            client, graph, task.approval_id, ApprovalDecision.APPROVE
        )
        assert done.status is TaskStatus.VERIFIED
        assert done.verification is not None
        assert done.verification.result.matched
    finally:
        client.close()

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"


def test_http_servers_reject_missing_or_wrong_bearer(http_servers):
    url = http_servers["inventory"]

    def status(headers: dict[str, str]) -> int:
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert status({}) == 401
    assert status({"Authorization": "Bearer wrong-token"}) == 401
    assert status({"Authorization": f"Bearer {TOKEN}"}) != 401


def test_unauthenticated_client_fails_safe(http_servers, settings: Settings):
    client = SplitMCPToolClient(
        http_settings(settings, http_servers),
        inventory_url=http_servers["inventory"],
        mall_url=http_servers["mall"],
        headers=None,
        connect_timeout=30.0,
    )
    graph = build_graph(
        client, SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    )
    try:
        first = client.check_inventory(
            CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
        )
        assert not first.ok
        assert first.error_code is ErrorCode.TOOL_UNAVAILABLE

        task = start_task(client, graph, request())
        assert task.status is TaskStatus.ABORTED_UNKNOWN_STATE
    finally:
        client.close()

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"
