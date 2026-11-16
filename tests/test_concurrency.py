"""Concurrency invariants: exactly one winner, no duplicate mall writes."""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest
import uvicorn
from conftest import count_rows, fetch_one

from stock_pilot import approval, mall
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import initialize_database
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    StagingStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    PublishListingInput,
    UpdatePriceInput,
)
from stock_pilot.tools import StockPilotTools


@pytest.fixture(autouse=True)
def _seeded_database(settings: Settings) -> None:
    StockPilotTools(settings)


def stage(settings: Settings, price: str = "59.00"):
    return mall.update_price(
        settings.db_path,
        UpdatePriceInput(
            listing_id="LST-001",
            new_price=Decimal(price),
            proposal_reason="并发测试",
        ),
    )


def race(*fns):
    """Start all callables at the same barrier and collect typed outcomes."""
    barrier = threading.Barrier(len(fns))

    def runner(fn):
        barrier.wait(timeout=10)
        try:
            return ("ok", fn())
        except ToolFailure as exc:
            return ("business", exc.code)

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = [pool.submit(runner, fn) for fn in fns]
        return [future.result(timeout=30) for future in futures]


def test_concurrent_initialization_seeds_once(tmp_path):
    """Split servers and the orchestrator may all open a fresh demo DB."""
    db_path = str(tmp_path / "concurrent-init.db")
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def initialize() -> None:
        barrier.wait(timeout=10)
        try:
            initialize_database(db_path)
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    conn = sqlite3.connect(db_path)
    try:
        inventory = conn.execute(
            "SELECT COUNT(*) FROM inventory_records"
        ).fetchone()[0]
        listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    finally:
        conn.close()
    assert inventory == 4
    assert listings == 4


def test_concurrent_staging_has_one_winner(settings: Settings):
    results = race(*[lambda: stage(settings) for _ in range(6)])

    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert all(
        code is ErrorCode.PENDING_APPROVAL_EXISTS
        for kind, code in results
        if kind != "ok"
    )
    assert count_rows(settings, "staging") == 1
    assert count_rows(settings, "approvals") == 1


def test_staging_race_repeats_cleanly(tmp_path):
    """No sleep-based timing: the same race must resolve one way every time."""
    for index in range(5):
        settings = Settings(
            db_path=str(tmp_path / f"race-{index}.db"),
            tool_timeout_seconds=2.0,
            read_max_retries=1,
            circuit_failure_threshold=3,
            circuit_cooldown_seconds=1.0,
        )
        StockPilotTools(settings)
        results = race(
            *[
                (lambda s=settings: stage(s))
                for _ in range(4)
            ]
        )
        assert sum(1 for kind, _ in results if kind == "ok") == 1


def test_concurrent_opposite_decisions_are_consistent(settings: Settings):
    staged = stage(settings)
    results = race(
        lambda: approval.decide_approval(
            settings.db_path,
            staged.approval_id,
            ApprovalDecision.APPROVE,
            "同意",
            "审批人A",
        ),
        lambda: approval.decide_approval(
            settings.db_path,
            staged.approval_id,
            ApprovalDecision.REJECT,
            "拒绝",
            "审批人B",
        ),
    )

    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert all(
        code is ErrorCode.APPROVAL_ALREADY_DECIDED
        for kind, code in results
        if kind != "ok"
    )

    record = approval.get_approval(settings.db_path, staged.approval_id)
    staging = approval.get_staging_for_approval(
        settings.db_path, staged.approval_id
    )
    assert record is not None and staging is not None
    if record.status is ApprovalStatus.APPROVED:
        assert staging.status is StagingStatus.PRICE_STAGED
    else:
        assert record.status is ApprovalStatus.REJECTED
        assert staging.status is StagingStatus.DISCARDED


def test_concurrent_publish_commits_once(settings: Settings):
    staged = stage(settings)
    approval.decide_approval(
        settings.db_path, staged.approval_id, ApprovalDecision.APPROVE, "同意"
    )

    def publish():
        return mall.publish_listing(
            settings.db_path,
            PublishListingInput(
                listing_id="LST-001", approval_id=staged.approval_id
            ),
        )

    results = race(*[publish for _ in range(5)])

    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert all(
        code in (ErrorCode.APPROVAL_ALREADY_CONSUMED, ErrorCode.STAGING_NOT_FOUND)
        for kind, code in results
        if kind != "ok"
    )

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"

    record = approval.get_approval(settings.db_path, staged.approval_id)
    staging = approval.get_staging_for_approval(
        settings.db_path, staged.approval_id
    )
    assert record is not None and record.status is ApprovalStatus.CONSUMED
    assert staging is not None and staging.status is StagingStatus.COMMITTED


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_concurrent_idempotent_submissions_create_one_task(settings: Settings):
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        if not thread.is_alive():
            raise RuntimeError("API server thread died during startup")
        time.sleep(0.02)
    assert server.started

    headers = {"Idempotency-Key": "race-key"}
    body = {
        "sku_id": "SKU-TSHIRT-001",
        "spec": "白色-M",
        "target_price": "59.00",
        "reason": "并发幂等测试",
    }
    barrier = threading.Barrier(5)

    def submit(_index: int):
        barrier.wait(timeout=10)
        with httpx.Client(timeout=60.0) as http:
            return http.post(
                f"http://127.0.0.1:{port}/tasks", json=body, headers=headers
            )

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(submit, range(5)))
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert sorted(response.status_code for response in responses) == [
        200,
        200,
        200,
        200,
        201,
    ]
    assert len({response.json()["task_id"] for response in responses}) == 1
    assert count_rows(settings, "tasks") == 1
    assert count_rows(settings, "approvals") == 1
