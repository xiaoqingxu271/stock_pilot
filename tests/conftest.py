from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot.agent import build_graph
from stock_pilot.config import Settings
from stock_pilot.tools import StockPilotTools


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "pilot.db"),
        tool_timeout_seconds=2.0,
        read_max_retries=1,
        circuit_failure_threshold=3,
        circuit_cooldown_seconds=1.0,
    )


@pytest.fixture
def tools(settings: Settings) -> StockPilotTools:
    return StockPilotTools(settings)


@pytest.fixture
def checkpointer():
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


@pytest.fixture
def graph(tools: StockPilotTools, checkpointer):
    return build_graph(tools, checkpointer)


def fetch_one(settings: Settings, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def count_rows(settings: Settings, table: str) -> int:
    row = fetch_one(settings, f"SELECT COUNT(*) AS n FROM {table}")
    assert row is not None
    return int(row["n"])
