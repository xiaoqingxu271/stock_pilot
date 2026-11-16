"""SQLite storage shared by the three simulated systems.

One database file, three module boundaries in code:
``inventory_records`` (inventory system), ``listings``/``staging`` (mall
system), ``approvals`` (approval queue). Cross-module commits (price +
publish + consume approval) happen in a single ``BEGIN IMMEDIATE``
transaction so they are atomic.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

INVENTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_records (
    id          TEXT PRIMARY KEY,
    sku_id      TEXT NOT NULL,
    spec        TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    reserved_by TEXT,
    reserved_at TEXT,
    UNIQUE (sku_id, spec)
);
"""

MALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                   TEXT PRIMARY KEY,
    sku_id               TEXT NOT NULL,
    title                TEXT NOT NULL,
    spec                 TEXT NOT NULL,
    price_cents          INTEGER NOT NULL,
    status               TEXT NOT NULL,
    inventory_record_id  TEXT NOT NULL,
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
    consumed_at          TEXT,
    abandoned_at         TEXT,
    abandon_reason       TEXT,
    abandoned_by         TEXT
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
"""

ORCHESTRATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    sku_id               TEXT NOT NULL,
    spec                 TEXT NOT NULL,
    target_price_cents   INTEGER NOT NULL,
    reason               TEXT NOT NULL,
    idempotency_key      TEXT,
    submitted_by         TEXT,
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

-- Notification-only outbox. Rows are enqueued in the same transaction as the
-- terminal task update and delivered at least once by a worker that has no
-- access to the tool client. It must never re-drive a write operation.
CREATE TABLE IF NOT EXISTS outbox_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT NOT NULL UNIQUE,
    topic         TEXT NOT NULL,
    task_id       TEXT,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    dead_lettered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(id) WHERE delivered_at IS NULL;
"""

SCHEMA = INVENTORY_SCHEMA + MALL_SCHEMA + ORCHESTRATOR_SCHEMA


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # WAL is persisted in the database header; another process may hold
        # the lock while initializing. Locked here is harmless.
        pass
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
    _ensure_parent(db_path)
    _create_schema(db_path, SCHEMA, _migrate_all)
    if seed:
        _seed_if_empty(db_path, "inventory_records", _seed_demo_data)


def initialize_split_stores(
    *,
    inventory_db_path: str,
    mall_db_path: str,
    orchestrator_db_path: str,
    seed: bool = True,
) -> None:
    """Create the three independent stores used by the split deployment."""
    _ensure_parent(inventory_db_path)
    _ensure_parent(mall_db_path)
    _ensure_parent(orchestrator_db_path)

    _create_schema(inventory_db_path, INVENTORY_SCHEMA, _migrate_inventory)
    _create_schema(mall_db_path, MALL_SCHEMA, _migrate_mall)
    _create_schema(
        orchestrator_db_path, ORCHESTRATOR_SCHEMA, _migrate_orchestrator
    )

    if seed:
        _seed_if_empty(
            inventory_db_path, "inventory_records", _seed_inventory_data
        )
        _seed_if_empty(mall_db_path, "listings", _seed_listing_data)


def _ensure_parent(db_path: str) -> None:
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def _create_schema(
    db_path: str, schema: str, migrate: Callable[[sqlite3.Connection], None]
) -> None:
    """Create tables and run migrations, tolerating concurrent starters.

    ``PRAGMA journal_mode=WAL`` and DDL can report "database is locked"
    without honoring the busy timeout. Split deployments legitimately boot
    several processes against the same fresh demo DB, so retry briefly.
    """
    attempts = 20
    for attempt in range(attempts):
        conn = connect(db_path)
        try:
            conn.executescript(schema)
            migrate(conn)
            return
        except sqlite3.OperationalError as exc:
            if attempt + 1 == attempts or "lock" not in str(exc).lower():
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))
        finally:
            conn.close()


def _seed_if_empty(
    db_path: str, table: str, seed: Callable[[sqlite3.Connection], None]
) -> None:
    """Seed once, even when several processes boot against a fresh store."""
    attempts = 20
    for attempt in range(attempts):
        try:
            with transaction(db_path) as tx:
                count = tx.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
                if count == 0:
                    seed(tx)
            return
        except sqlite3.OperationalError as exc:
            if attempt + 1 == attempts or "lock" not in str(exc).lower():
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_all(conn: sqlite3.Connection) -> None:
    _migrate_inventory(conn)
    _migrate_mall(conn)
    _migrate_orchestrator(conn)


def _migrate_inventory(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "inventory_records", "reserved_by", "reserved_by TEXT")
    _ensure_column(conn, "inventory_records", "reserved_at", "reserved_at TEXT")


def _migrate_mall(conn: sqlite3.Connection) -> None:
    """Additive migrations so an existing demo database keeps working."""
    _ensure_column(conn, "approvals", "decided_by", "decided_by TEXT")
    _ensure_column(conn, "approvals", "abandoned_at", "abandoned_at TEXT")
    _ensure_column(conn, "approvals", "abandon_reason", "abandon_reason TEXT")
    _ensure_column(conn, "approvals", "abandoned_by", "abandoned_by TEXT")


def _migrate_orchestrator(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "tasks", "idempotency_key", "idempotency_key TEXT")
    _ensure_column(conn, "tasks", "submitted_by", "submitted_by TEXT")
    _ensure_column(
        conn, "outbox_events", "dead_lettered_at", "dead_lettered_at TEXT"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idempotency_key"
        " ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def _inventory_fixtures() -> list[tuple[str, str, str, int]]:
    return [
        # happy path: in stock
        ("INV-001", "SKU-TSHIRT-001", "白色-M", 20),
        # ambiguity: one sku, two same-title listings / two specs
        ("INV-002", "SKU-AMBIG-002", "白色-M", 15),
        ("INV-003", "SKU-AMBIG-002", "白色-L", 8),
        # insufficient stock
        ("INV-004", "SKU-HOODIE-003", "黑色-L", 0),
    ]


def _listing_fixtures() -> list[tuple[str, str, str, str, int, str, str]]:
    return [
        ("LST-001", "SKU-TSHIRT-001", "经典白T恤", "白色-M", 7900, "draft", "INV-001"),
        ("LST-002", "SKU-AMBIG-002", "经典白T恤", "白色-M", 8900, "draft", "INV-002"),
        ("LST-003", "SKU-AMBIG-002", "经典白T恤", "白色-L", 9200, "draft", "INV-003"),
        ("LST-004", "SKU-HOODIE-003", "连帽卫衣", "黑色-L", 19900, "draft", "INV-004"),
    ]


def _seed_inventory_data(conn: sqlite3.Connection) -> None:
    now = iso(utcnow())
    conn.executemany(
        "INSERT INTO inventory_records (id, sku_id, spec, quantity, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [row + (now,) for row in _inventory_fixtures()],
    )


def _seed_listing_data(conn: sqlite3.Connection) -> None:
    now = iso(utcnow())
    conn.executemany(
        "INSERT INTO listings (id, sku_id, title, spec, price_cents, status,"
        " inventory_record_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [row + (now,) for row in _listing_fixtures()],
    )


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    """Deterministic fixtures used by the acceptance tests and the demo."""
    _seed_inventory_data(conn)
    _seed_listing_data(conn)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # Older rows (or hand-edited demo data) may lack an offset. Every
        # timestamp StockPilot writes is UTC, so assume UTC rather than
        # crashing on aware/naive arithmetic later.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
