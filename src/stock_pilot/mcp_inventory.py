"""Inventory MCP server over Streamable HTTP (read-only tools)."""

from __future__ import annotations

import os

from stock_pilot.mcp_http import run_http_server
from stock_pilot.mcp_server import build_inventory_server


def main() -> None:
    run_http_server(
        build_inventory_server(),
        host=os.environ.get("STOCK_PILOT_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("STOCK_PILOT_INVENTORY_PORT", "8101")),
        token=os.environ.get("STOCK_PILOT_AGENT_TOKEN") or None,
    )


if __name__ == "__main__":
    main()
