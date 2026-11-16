"""Runtime configuration.

Every tool call is bounded by a timeout; read-only tools may retry and are
guarded by a per-tool circuit breaker. Writes never retry blindly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "stock_pilot.db"


@dataclass(frozen=True)
class Settings:
    db_path: str = str(DEFAULT_DB_PATH)
    tool_timeout_seconds: float = 5.0
    read_max_retries: int = 2
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 30.0
    # Production split: two HTTP MCP servers over a shared demo database.
    inventory_mcp_url: str = "http://127.0.0.1:8101/mcp"
    mall_mcp_url: str = "http://127.0.0.1:8102/mcp"
    # Static bearer tokens. With no tokens configured the demo runs in
    # unauthenticated dev mode; configuring any token enforces role checks.
    agent_token: str | None = None
    operator_token: str | None = None
    admin_token: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=os.environ.get("STOCK_PILOT_DB", str(DEFAULT_DB_PATH)),
            tool_timeout_seconds=float(os.environ.get("STOCK_PILOT_TOOL_TIMEOUT", "5")),
            read_max_retries=int(os.environ.get("STOCK_PILOT_READ_RETRIES", "2")),
            circuit_failure_threshold=int(
                os.environ.get("STOCK_PILOT_CIRCUIT_THRESHOLD", "3")
            ),
            circuit_cooldown_seconds=float(
                os.environ.get("STOCK_PILOT_CIRCUIT_COOLDOWN", "30")
            ),
            inventory_mcp_url=os.environ.get(
                "STOCK_PILOT_INVENTORY_URL", "http://127.0.0.1:8101/mcp"
            ),
            mall_mcp_url=os.environ.get(
                "STOCK_PILOT_MALL_URL", "http://127.0.0.1:8102/mcp"
            ),
            agent_token=os.environ.get("STOCK_PILOT_AGENT_TOKEN") or None,
            operator_token=os.environ.get("STOCK_PILOT_OPERATOR_TOKEN") or None,
            admin_token=os.environ.get("STOCK_PILOT_ADMIN_TOKEN") or None,
        )
