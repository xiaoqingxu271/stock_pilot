"""Reservation TTL and sweep semantics.

The sweep is deliberately conservative: it releases reservations whose
approval can no longer publish (consumed / rejected / abandoned / missing) or
whose task is terminal. An approved reservation with a live task is retained
even past its advisory TTL, and reported as stale for an operator.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from helpers import wait_until
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot import tasks
from stock_pilot.agent import (
    build_graph,
    decide_and_resume,
    resume_task,
    start_task,
)
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import iso, utcnow
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    RecoveryKind,
    ReservationGcAction,
    TaskStatus,
)
from stock_pilot.metrics import render_metrics
from stock_pilot.models import (
    CreateTaskRequest,
    ReleaseInventoryInput,
    ReserveInventoryInput,
)
from stock_pilot.recovery import scan
from stock_pilot.reservations import ReservationSweeper, disposition, sweep
from stock_pilot.tools import StockPilotTools


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "orchestrator.db"),
        db_layout="split",
        tool_timeout_seconds=2.0,
        read_max_retries=1,
        circuit_failure_threshold=3,
        circuit_cooldown_seconds=1.0,
        reservation_ttl_seconds=900,
    )


@pytest.fixture
def tools(settings: Settings) -> StockPilotTools:
    return StockPilotTools(settings)


@pytest.fixture
def graph(tools: StockPilotTools):
    return build_graph(
        tools, SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    )


def request() -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id="SKU-TSHIRT-001",
        spec="白色-M",
        target_price=Decimal("59.00"),
        reason="预留清理测试",
    )


def set_reservation(path: str, approval_id: str, reserved_at: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE inventory_records SET reserved_by = ?, reserved_at = ?"
            " WHERE id = 'INV-001'",
            (approval_id, reserved_at),
        )
        conn.commit()
    finally:
        conn.close()


def reserved_by(path: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT reserved_by FROM inventory_records WHERE id = 'INV-001'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_disposition_is_conservative_for_live_approvals():
    retained, _ = disposition(
        ApprovalStatus.APPROVED, TaskStatus.AWAITING_APPROVAL, 10, 900
    )
    assert retained is ReservationGcAction.RETAINED

    stale, _ = disposition(
        ApprovalStatus.APPROVED, TaskStatus.AWAITING_APPROVAL, 901, 900
    )
    assert stale is ReservationGcAction.STALE

    consumed, _ = disposition(
        ApprovalStatus.CONSUMED, TaskStatus.VERIFIED, 10, 900
    )
    assert consumed is ReservationGcAction.RELEASED

    terminal, _ = disposition(
        ApprovalStatus.APPROVED, TaskStatus.ROLLED_BACK, 10, 900
    )
    assert terminal is ReservationGcAction.RELEASED

    missing, _ = disposition(None, None, 0, 900)
    assert missing is ReservationGcAction.RELEASED


def test_reserve_output_carries_advisory_expiry(tools: StockPilotTools):
    result = tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id="apr-expiry"
        )
    )
    assert result.ok and result.reserved
    assert result.reserved_at is not None
    assert result.expires_at is not None
    assert (
        result.expires_at - result.reserved_at
    ).total_seconds() == 900


def test_sweep_releases_consumed_reservation_once(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    done = decide_and_resume(
        tools, graph, task.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.VERIFIED
    assert reserved_by(settings.store_inventory) is None

    # Simulate a crash that leaked the reservation after the commit.
    set_reservation(settings.store_inventory, task.approval_id, iso(utcnow()))

    first = sweep(tools)
    assert first.released == 1
    assert first.items[0].action is ReservationGcAction.RELEASED
    assert reserved_by(settings.store_inventory) is None

    second = sweep(tools)
    assert second.released == 0
    assert second.items == []


def test_sweep_releases_abandoned_and_missing_approvals(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    tools.decide_approval(task.approval_id, ApprovalDecision.APPROVE)
    tools.abandon_approval(task.approval_id, "测试撤回")
    set_reservation(settings.store_inventory, task.approval_id, iso(utcnow()))

    result = sweep(tools)
    assert result.released == 1
    assert result.items[0].approval_status.value == "abandoned"

    set_reservation(settings.store_inventory, "apr-ghost", iso(utcnow()))
    missing = sweep(tools)
    assert missing.released == 1
    assert missing.items[0].approval_status is None
    assert "审批记录缺失" in missing.items[0].reason
    assert reserved_by(settings.store_inventory) is None


def test_sweep_retains_active_reservation_and_publish_completes(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    tools.decide_approval(task.approval_id, ApprovalDecision.APPROVE)
    reserve = tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id=task.approval_id
        )
    )
    assert reserve.ok

    result = sweep(tools)
    assert result.retained == 1
    assert result.released == 0
    assert reserved_by(settings.store_inventory) == task.approval_id

    # The sweep did not steal the reservation; the approved task still commits.
    done = decide_and_resume(
        tools, graph, task.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.VERIFIED
    assert reserved_by(settings.store_inventory) is None


def test_sweep_releases_reservation_behind_terminal_task(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    tools.decide_approval(task.approval_id, ApprovalDecision.APPROVE)
    tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id=task.approval_id
        )
    )
    tasks.update_task(
        settings.store_orchestrator, task.task_id, status=TaskStatus.ROLLED_BACK
    )

    result = sweep(tools)
    assert result.released == 1
    assert result.items[0].task_status is TaskStatus.ROLLED_BACK
    assert reserved_by(settings.store_inventory) is None


def test_stale_reservation_is_reported_but_not_released(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    tools.decide_approval(task.approval_id, ApprovalDecision.APPROVE)
    old = iso(utcnow() - timedelta(hours=2))
    set_reservation(settings.store_inventory, task.approval_id, old)

    result = sweep(tools)
    assert result.stale == 1
    assert result.items[0].action is ReservationGcAction.STALE
    assert reserved_by(settings.store_inventory) == task.approval_id

    issues = scan(
        settings.store_orchestrator,
        mall_db_path=settings.store_mall,
        inventory_db_path=settings.store_inventory,
        reservation_ttl_seconds=900,
    )
    stale = [issue for issue in issues if issue.kind is RecoveryKind.RESERVATION_STALE]
    assert len(stale) == 1
    assert stale[0].inventory_record_id == "INV-001"
    assert stale[0].reserved_at is not None

    # Cleanup so the fixture graph can still finish.
    tools.release_inventory(
        ReleaseInventoryInput(approval_id=task.approval_id)
    )
    resume_task(tools, graph, task.task_id, {"recovered": True})


def test_recovery_scan_reports_orphaned_reservation(settings: Settings):
    StockPilotTools(settings)
    set_reservation(settings.store_inventory, "apr-ghost", iso(utcnow()))

    issues = scan(
        settings.store_orchestrator,
        mall_db_path=settings.store_mall,
        inventory_db_path=settings.store_inventory,
        reservation_ttl_seconds=900,
    )
    orphaned = [
        issue
        for issue in issues
        if issue.kind is RecoveryKind.RESERVATION_ORPHANED
    ]
    assert len(orphaned) == 1
    assert orphaned[0].approval_id == "apr-ghost"
    assert orphaned[0].inventory_record_id == "INV-001"
    assert orphaned[0].sku_id == "SKU-TSHIRT-001"


def test_sweep_endpoint_requires_operator_and_updates_metrics(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "orchestrator.db"),
        db_layout="split",
        operator_token="op-token",
        reservation_ttl_seconds=900,
    )
    StockPilotTools(settings)
    set_reservation(settings.store_inventory, "apr-ghost", iso(utcnow()))

    with TestClient(create_app(settings)) as client:
        denied = client.post("/reservations/gc")
        assert denied.status_code == 401

        listed = client.get(
            "/reservations", headers={"Authorization": "Bearer op-token"}
        )
        assert listed.status_code == 200
        assert len(listed.json()["reservations"]) == 1

        metrics = client.get("/metrics").text
        assert "stock_pilot_reservations_open 1.0" in metrics
        assert "stock_pilot_reservations_stale 0.0" in metrics

        swept = client.post(
            "/reservations/gc",
            headers={"Authorization": "Bearer op-token"},
        )
        assert swept.status_code == 200
        body = swept.json()
        assert body["released"] == 1
        assert body["items"][0]["action"] == "released"

        assert "stock_pilot_reservations_open 0.0" in client.get("/metrics").text


def test_naive_legacy_reservation_timestamp_is_treated_as_utc(
    settings: Settings, tools: StockPilotTools
):
    StockPilotTools(settings)
    set_reservation(settings.store_inventory, "apr-ghost", "2020-01-01 00:00:00")

    listed = tools.list_reservations()
    assert listed.ok, listed
    assert listed.reservations[0].age_seconds > 0

    issues = scan(
        settings.store_orchestrator,
        mall_db_path=settings.store_mall,
        inventory_db_path=settings.store_inventory,
    )
    assert any(
        issue.kind is RecoveryKind.RESERVATION_ORPHANED for issue in issues
    ), issues


def test_reservation_endpoints_are_noops_in_shared_layout(tmp_path):
    settings = Settings(db_path=str(tmp_path / "stock_pilot.db"))
    with TestClient(create_app(settings)) as client:
        listed = client.get("/reservations")
        assert listed.status_code == 200
        assert listed.json()["reservations"] == []

        swept = client.post("/reservations/gc")
        assert swept.status_code == 200
        assert swept.json()["released"] == 0


def test_settings_reject_negative_gc_interval(tmp_path):
    with pytest.raises(ValueError, match="reservation_gc_interval_seconds"):
        Settings(
            db_path=str(tmp_path / "stock_pilot.db"),
            reservation_gc_interval_seconds=-1,
        )


def test_background_sweeper_releases_orphan_and_stops(settings: Settings):
    tools = StockPilotTools(settings)
    set_reservation(settings.store_inventory, "apr-ghost", iso(utcnow()))

    sweeper = ReservationSweeper(
        tools, interval_seconds=0.05, ttl_seconds=900
    )
    with sweeper:
        assert wait_until(
            lambda: reserved_by(settings.store_inventory) is None, timeout=5
        )
    assert sweeper._thread is None


def test_background_sweeper_never_steals_a_live_hold(
    settings: Settings, tools: StockPilotTools, graph
):
    task = start_task(tools, graph, request())
    tools.decide_approval(task.approval_id, ApprovalDecision.APPROVE)
    tools.reserve_inventory(
        ReserveInventoryInput(
            inventory_record_id="INV-001", approval_id=task.approval_id
        )
    )

    sweeper = ReservationSweeper(tools, interval_seconds=60, ttl_seconds=900)
    result = sweeper.run_once()

    assert result.retained == 1
    assert result.released == 0
    assert reserved_by(settings.store_inventory) == task.approval_id


def test_api_lifespan_starts_sweeper_only_when_configured(tmp_path):
    shared = Settings(db_path=str(tmp_path / "shared.db"))
    shared_app = create_app(shared)
    with TestClient(shared_app):
        assert shared_app.state.reservation_sweeper is None

    disabled = Settings(
        db_path=str(tmp_path / "split-disabled.db"),
        db_layout="split",
        reservation_gc_interval_seconds=0,
    )
    disabled_app = create_app(disabled)
    with TestClient(disabled_app):
        assert disabled_app.state.reservation_sweeper is None

    enabled = Settings(
        db_path=str(tmp_path / "split-enabled.db"),
        db_layout="split",
        reservation_gc_interval_seconds=0.05,
        reservation_ttl_seconds=900,
    )
    StockPilotTools(enabled)
    set_reservation(enabled.store_inventory, "apr-ghost", iso(utcnow()))

    enabled_app = create_app(enabled)
    with TestClient(enabled_app):
        assert enabled_app.state.reservation_sweeper is not None
        assert wait_until(
            lambda: reserved_by(enabled.store_inventory) is None, timeout=10
        )


def test_metrics_render_with_reservation_gauges(settings: Settings):
    StockPilotTools(settings)
    set_reservation(settings.store_inventory, "apr-ghost", iso(utcnow()))
    text = render_metrics(
        settings.store_orchestrator,
        mall_db_path=settings.store_mall,
        inventory_db_path=settings.store_inventory,
        reservation_ttl_seconds=settings.reservation_ttl_seconds,
    ).decode()
    assert "stock_pilot_reservations_open 1.0" in text
    assert "stock_pilot_recovery_issues 1.0" in text
