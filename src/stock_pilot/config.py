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

    @classmethod
    def from_env(cls) -> "Settings":
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
        )
