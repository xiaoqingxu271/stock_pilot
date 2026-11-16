"""MCP server exposing the four contracted tools.

MCP 2.x renamed ``FastMCP`` to ``MCPServer``. Tools accept flat arguments
(the MCP wire contract) and immediately build the Pydantic input model, so
all validation and output schemas are Pydantic-based.

The same registration helpers back three deployments:

* ``build_mcp_server`` - one stdio server with all four tools (default demo)
* ``build_inventory_server`` - read-only inventory tools for the split setup
* ``build_mall_server`` - listing lookup plus both approval-gated writes
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.mcpserver import MCPServer

from stock_pilot.config import Settings
from stock_pilot.models import (
    CheckInventoryInput,
    CheckInventoryOutput,
    GetListingInput,
    GetListingOutput,
    ListReservationsOutput,
    PublishListingInput,
    PublishListingOutput,
    ReleaseInventoryInput,
    ReleaseInventoryOutput,
    ReserveInventoryInput,
    ReserveInventoryOutput,
    UpdatePriceInput,
    UpdatePriceOutput,
)
from stock_pilot.tools import StockPilotTools

READ_ONLY_INSTRUCTIONS = (
    "Read-only tools. Never infer or guess; report ambiguity instead."
)


def _new_server(name: str, instructions: str) -> MCPServer:
    return MCPServer(name=name, instructions=instructions)


def register_inventory_tools(server: MCPServer, tools: StockPilotTools) -> None:
    @server.tool(
        name="check_inventory",
        description=(
            "Read inventory for an exact sku_id + spec. Returns quantity and "
            "timestamp; quantity <= 0 must terminate the task with zero writes."
        ),
        structured_output=True,
    )
    async def check_inventory(sku_id: str, spec: str) -> CheckInventoryOutput:
        request = CheckInventoryInput(sku_id=sku_id, spec=spec)
        return await asyncio.to_thread(tools.check_inventory, request)


def register_inventory_saga_tools(
    server: MCPServer, tools: StockPilotTools
) -> None:
    """Internal reserve/release/list steps used only by the split deployment.

    These are not part of the four model-facing tools; they close the
    recheck-to-commit race without giving the model a write primitive, and
    let the orchestrator sweep reservations left by a crash.
    """

    @server.tool(
        name="reserve_inventory",
        description=(
            "Internal saga step. Hold stock for an approved proposal until "
            "the mall commit completes or is abandoned."
        ),
        structured_output=True,
    )
    async def reserve_inventory(
        inventory_record_id: str, approval_id: str
    ) -> ReserveInventoryOutput:
        request = ReserveInventoryInput(
            inventory_record_id=inventory_record_id, approval_id=approval_id
        )
        return await asyncio.to_thread(tools.reserve_inventory, request)

    @server.tool(
        name="release_inventory",
        description=(
            "Internal saga step. Release a stock reservation; idempotent."
        ),
        structured_output=True,
    )
    async def release_inventory(approval_id: str) -> ReleaseInventoryOutput:
        request = ReleaseInventoryInput(approval_id=approval_id)
        return await asyncio.to_thread(tools.release_inventory, request)

    @server.tool(
        name="list_reservations",
        description=(
            "Internal saga read. List live stock reservations with their age "
            "and advisory expiry; never releases anything."
        ),
        structured_output=True,
    )
    async def list_reservations() -> ListReservationsOutput:
        return await asyncio.to_thread(tools.list_reservations)


def register_mall_tools(server: MCPServer, tools: StockPilotTools) -> None:
    @server.tool(
        name="get_listing",
        description=(
            "Read listing mapping. Returns resolved / ambiguous / not_found. "
            "When ambiguous, return the candidate list and stop; never choose."
        ),
        structured_output=True,
    )
    async def get_listing(sku_id: str) -> GetListingOutput:
        request = GetListingInput(sku_id=sku_id)
        return await asyncio.to_thread(tools.get_listing, request)

    @server.tool(
        name="update_price",
        description=(
            "Stage a combined price+publish proposal in the approval queue. "
            "Does not change the public price. Returns approval_id."
        ),
        structured_output=True,
    )
    async def update_price(
        listing_id: str, new_price: str, proposal_reason: str
    ) -> UpdatePriceOutput:
        # new_price is passed as a string on the wire to preserve decimals.
        request = UpdatePriceInput(
            listing_id=listing_id,
            new_price=new_price,
            proposal_reason=proposal_reason,
        )
        return await asyncio.to_thread(tools.update_price, request)

    @server.tool(
        name="publish_listing",
        description=(
            "Atomically commit the approved staged price and publish the "
            "listing. Requires an approved approval_id; call only after "
            "update_price completed."
        ),
        structured_output=True,
    )
    async def publish_listing(
        listing_id: str, approval_id: str
    ) -> PublishListingOutput:
        request = PublishListingInput(
            listing_id=listing_id, approval_id=approval_id
        )
        return await asyncio.to_thread(tools.publish_listing, request)


def _server_tools() -> StockPilotTools:
    return StockPilotTools(Settings.from_env(), record_events=False)


def build_inventory_server(tools: StockPilotTools | None = None) -> MCPServer:
    tools = tools or _server_tools()
    server = _new_server("stock-pilot-inventory", READ_ONLY_INSTRUCTIONS)
    register_inventory_tools(server, tools)
    if tools.settings.split_stores:
        register_inventory_saga_tools(server, tools)
    return server


def build_mall_server(tools: StockPilotTools | None = None) -> MCPServer:
    tools = tools or _server_tools()
    server = _new_server(
        "stock-pilot-mall",
        (
            "Listing lookup and two-phase repricing. Reads are safe; the two "
            "write tools only ever act through the approval queue."
        ),
    )
    register_mall_tools(server, tools)
    return server


def build_mcp_server(tools: StockPilotTools | None = None) -> MCPServer:
    tools = tools or _server_tools()
    server = _new_server(
        "stock-pilot",
        (
            "Inventory-driven repricing. Reads are safe; update_price and "
            "publish_listing only ever act through the approval queue. Never "
            "guess when get_listing returns ambiguous."
        ),
    )
    register_inventory_tools(server, tools)
    if tools.settings.split_stores:
        register_inventory_saga_tools(server, tools)
    register_mall_tools(server, tools)
    return server


def main() -> None:
    from stock_pilot.logging_config import configure_logging

    configure_logging()
    transport = os.environ.get("STOCK_PILOT_MCP_TRANSPORT", "stdio")
    build_mcp_server().run(transport=transport)


if __name__ == "__main__":
    main()
