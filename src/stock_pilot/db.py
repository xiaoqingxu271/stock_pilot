"""SQLite storage shared by the three simulated systems.

One database file, three module boundaries in code:
``inventory_records`` (inventory system), ``listings``/``staging`` (mall
system), ``approvals`` (approval queue). Cross-module commits (price +
publish + consume approval) happen in a single ``BEGIN IMMEDIATE``
transaction so they are atomic.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_records (
    id          TEXT PRIMARY KEY,
    sku_id      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (sku_id, spec)
);

CREATE TABLE IF NOT EXISTS listings (
    id                   TEXT PRIMARY KEY,
    sku_id               TEXT NOT NULL,
    title                TEXT NOT NULL,
    spec                 TEXT NOT NULL,
    price_cents          INTEGER NOT NULL,
    status               TEXT NOT NULL,
    inventory_record_id  TEXT NOT NULL REFERENCES inventory_records(id),
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_sku ON listings(sku_id);

CREATE TABLE IF NOT EXISTS approvals (
    id                   TEXT PRIMARY KEY,
    listing_id           TEXT NOT NULL,
    action               TEXT NOT NULL,
    new_price_cents      INTEGER NOT NULL,
    original_price_cents INTEGER NOT NULL,
    proposal_reason      TEXT NOT NULL,
    status               TEXT NOT NULL,
    decision_note        TEXT,
    decided_by           TEXT,
    created_at           TEXT NOT NULL,
    decided_at           TEXT,
    consumed_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS staging (
    id                   TEXT PRIMARY KEY,
    listing_id           TEXT NOT NULL,
    approval_id          TEXT NOT NULL UNIQUE,
    new_price_cents      INTEGER NOT NULL,
    original_price_cents INTEGER NOT NULL,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
-- At most one live price proposal per listing.
CREATE UNIQUE INDEX IF NOT EXISTS uq_staging_active_listing
    ON staging(listing_id) WHERE status = 'price_staged';

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    sku_id               TEXT NOT NULL,
    spec                 TEXT NOT NULL,
    target_price_cents   INTEGER NOT NULL,
    reason               TEXT NOT NULL,
    idempotency_key      TEXT,
    status               TEXT NOT NULL,
    listing_id           TEXT,
    approval_id          TEXT,
    original_price_cents INTEGER,
    final_price_cents    INTEGER,
    candidates_json      TEXT,
    verification_json    TEXT,
    error_code           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS tool_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    tool        TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    outcome     TEXT NOT NULL,
    error_code  TEXT,
    latency_ms  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_events_task ON tool_events(task_id, id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Atomic write transaction with rollback on any exception."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database(db_path: str, *, seed: bool = True) -> None:
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM inventory_records"
        ).fetchone()["n"]
    finally:
        conn.close()

    if seed and count == 0:
        with transaction(db_path) as tx:
            _seed_demo_data(tx)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations so an existing demo database keeps working."""
    _ensure_column(conn, "approvals", "decided_by", "decided_by TEXT")
    _ensure_column(conn, "tasks", "idempotency_key", "idempotency_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idempotency_key"
        " ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    """Deterministic fixtures used by the acceptance tests and the demo."""
    now = iso(utcnow())

    inventory = [
        # happy path: in stock
        ("INV-001", "SKU-TSHIRT-001", "白色-M", 20),
        # ambiguity: one sku, two same-title listings / two specs
        ("INV-002", "SKU-AMBIG-002", "白色-M", 15),
        ("INV-003", "SKU-AMBIG-002", "白色-L", 8),
        # insufficient stock
        ("INV-004", "SKU-HOODIE-003", "黑色-L", 0),
    ]
    conn.executemany(
        "INSERT INTO inventory_records (id, sku_id, spec, quantity, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(i, sku, spec, qty, now) for i, sku, spec, qty in inventory],
    )

    listings = [
        ("LST-001", "SKU-TSHIRT-001", "经典白T恤", "白色-M", 7900, "draft", "INV-001"),
        ("LST-002", "SKU-AMBIG-002", "经典白T恤", "白色-M", 8900, "draft", "INV-002"),
        ("LST-003", "SKU-AMBIG-002", "经典白T恤", "白色-L", 9200, "draft", "INV-003"),
        ("LST-004", "SKU-HOODIE-003", "连帽卫衣", "黑色-L", 19900, "draft", "INV-004"),
    ]
    conn.executemany(
        "INSERT INTO listings (id, sku_id, title, spec, price_cents, status,"
        " inventory_record_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [row + (now,) for row in listings],
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
