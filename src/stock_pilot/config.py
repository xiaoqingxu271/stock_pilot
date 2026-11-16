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
    # shared = one SQLite file for all modules (demo default);
    # split = inventory / mall(+approvals) / orchestrator each own a file.
    db_layout: str = "shared"
    inventory_db_path: str | None = None
    mall_db_path: str | None = None
    tool_timeout_seconds: float = 5.0
    read_max_retries: int = 2
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 30.0
    # Advisory age after which a live stock reservation is flagged for an
    # operator. It never triggers an automatic release: an approved proposal
    # may still be about to publish.
    reservation_ttl_seconds: int = 900
    # Production split: two HTTP MCP servers over a shared demo database.
    inventory_mcp_url: str = "http://127.0.0.1:8101/mcp"
    mall_mcp_url: str = "http://127.0.0.1:8102/mcp"
    # Static bearer tokens. With no tokens configured the demo runs in
    # unauthenticated dev mode; configuring any token enforces role checks.
    agent_token: str | None = None
    operator_token: str | None = None
    admin_token: str | None = None
    # Read endpoints are open for the local console by default. Turn this on
    # for production-like deployments that require a token even to read.
    require_read_auth: bool = False

    def __post_init__(self) -> None:
        if self.db_layout not in {"shared", "split"}:
            raise ValueError(
                f"db_layout must be 'shared' or 'split', got {self.db_layout!r}"
            )
        if self.reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be positive")

    @property
    def split_stores(self) -> bool:
        return self.db_layout == "split"

    def _sibling(self, name: str) -> str:
        return str(Path(self.db_path).with_name(f"{name}.db"))

    @property
    def store_orchestrator(self) -> str:
        return self.db_path

    @property
    def store_inventory(self) -> str:
        if not self.split_stores:
            return self.db_path
        return self.inventory_db_path or self._sibling("inventory")

    @property
    def store_mall(self) -> str:
        if not self.split_stores:
            return self.db_path
        return self.mall_db_path or self._sibling("mall")

    @property
    def approval_db(self) -> str:
        """The approval queue gates mall writes, so it lives with the mall."""
        return self.store_mall

    @classmethod
    def from_env(cls) -> Settings:
        layout = os.environ.get("STOCK_PILOT_DB_LAYOUT", "shared").strip().lower()
        return cls(
            db_path=os.environ.get("STOCK_PILOT_DB", str(DEFAULT_DB_PATH)),
            db_layout=layout,
            inventory_db_path=os.environ.get("STOCK_PILOT_INVENTORY_DB") or None,
            mall_db_path=os.environ.get("STOCK_PILOT_MALL_DB") or None,
            tool_timeout_seconds=float(os.environ.get("STOCK_PILOT_TOOL_TIMEOUT", "5")),
            read_max_retries=int(os.environ.get("STOCK_PILOT_READ_RETRIES", "2")),
            circuit_failure_threshold=int(
                os.environ.get("STOCK_PILOT_CIRCUIT_THRESHOLD", "3")
            ),
            circuit_cooldown_seconds=float(
                os.environ.get("STOCK_PILOT_CIRCUIT_COOLDOWN", "30")
            ),
            reservation_ttl_seconds=int(
                os.environ.get("STOCK_PILOT_RESERVATION_TTL", "900")
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
            require_read_auth=os.environ.get(
                "STOCK_PILOT_REQUIRE_READ_AUTH", ""
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        )
