"""MCP server exposing the four contracted tools.

MCP 2.x renamed ``FastMCP`` to ``MCPServer``. Tools accept flat arguments
(the MCP wire contract) and immediately build the Pydantic input model, so
all validation and output schemas are Pydantic-based.
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.mcpserver import MCPServer

from stock_pilot.models import (
    CheckInventoryInput,
    CheckInventoryOutput,
    GetListingInput,
    GetListingOutput,
    PublishListingInput,
    PublishListingOutput,
    UpdatePriceInput,
    UpdatePriceOutput,
)
from stock_pilot.tools import StockPilotTools


def build_mcp_server(tools: StockPilotTools | None = None) -> MCPServer:
    tools = tools or StockPilotTools()
    server = MCPServer(
        name="stock-pilot",
        instructions=(
            "Inventory-driven repricing. Reads are safe; update_price and "
            "publish_listing only ever act through the approval queue. Never "
            "guess when get_listing returns ambiguous."
        ),
    )

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

    return server


def main() -> None:
    transport = os.environ.get("STOCK_PILOT_MCP_TRANSPORT", "stdio")
    build_mcp_server().run(transport=transport)


if __name__ == "__main__":
    main()
