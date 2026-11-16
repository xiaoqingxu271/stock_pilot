"""Inventory system module.

``check_inventory`` is the model-facing read tool. ``reserve_inventory`` and
``release_inventory`` are internal saga steps used only by the split-store
deployment: they let the orchestrator hold stock across the gap between the
publish-time recheck and the mall commit. They are not offered to the model.
"""

from __future__ import annotations

from datetime import timedelta

from stock_pilot.db import connect, iso, parse_dt, transaction, utcnow
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    CheckInventoryInput,
    CheckInventoryOutput,
    ListReservationsOutput,
    ReleaseInventoryInput,
    ReleaseInventoryOutput,
    ReservationView,
    ReserveInventoryInput,
    ReserveInventoryOutput,
)


def check_inventory(
    db_path: str, request: CheckInventoryInput
) -> CheckInventoryOutput:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, sku_id, spec, quantity, updated_at"
            " FROM inventory_records WHERE sku_id = ? AND spec = ?",
            (request.sku_id, request.spec),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ToolFailure(
            ErrorCode.INVENTORY_NOT_FOUND,
            f"no inventory record for sku_id={request.sku_id!r} spec={request.spec!r}",
        )

    return CheckInventoryOutput(
        sku_id=row["sku_id"],
        spec=row["spec"],
        quantity=row["quantity"],
        timestamp=parse_dt(row["updated_at"]),
        inventory_record_id=row["id"],
    )


def reserve_inventory(
    db_path: str, request: ReserveInventoryInput
) -> ReserveInventoryOutput:
    """Hold stock for one approval; idempotent for the same approval."""
    now = iso(utcnow())
    with transaction(db_path) as tx:
        row = tx.execute(
            "SELECT id, quantity, reserved_by, reserved_at"
            " FROM inventory_records WHERE id = ?",
            (request.inventory_record_id,),
        ).fetchone()
        if row is None:
            raise ToolFailure(
                ErrorCode.INVENTORY_NOT_FOUND,
                f"inventory record {request.inventory_record_id!r} does not exist",
            )
        if row["reserved_by"] == request.approval_id:
            return ReserveInventoryOutput(
                inventory_record_id=row["id"],
                approval_id=request.approval_id,
                quantity=row["quantity"],
                reserved=True,
                reserved_at=(
                    parse_dt(row["reserved_at"]) if row["reserved_at"] else None
                ),
            )
        if row["reserved_by"] is not None:
            raise ToolFailure(
                ErrorCode.STOCK_RESERVED,
                f"inventory record {row['id']!r} is reserved by another approval",
            )
        if row["quantity"] <= 0:
            raise ToolFailure(
                ErrorCode.INSUFFICIENT_STOCK,
                f"inventory record {row['id']!r} has no stock to reserve",
            )
        tx.execute(
            "UPDATE inventory_records SET reserved_by = ?, reserved_at = ?"
            " WHERE id = ?",
            (request.approval_id, now, row["id"]),
        )
    return ReserveInventoryOutput(
        inventory_record_id=row["id"],
        approval_id=request.approval_id,
        quantity=row["quantity"],
        reserved=True,
        reserved_at=parse_dt(now),
    )


def release_inventory(
    db_path: str, request: ReleaseInventoryInput
) -> ReleaseInventoryOutput:
    """Release a reservation; safe to call when nothing is reserved."""
    with transaction(db_path) as tx:
        tx.execute(
            "UPDATE inventory_records SET reserved_by = NULL, reserved_at = NULL"
            " WHERE reserved_by = ?",
            (request.approval_id,),
        )
    return ReleaseInventoryOutput(
        approval_id=request.approval_id, released=True
    )


def list_reservations(
    db_path: str, ttl_seconds: int
) -> ListReservationsOutput:
    """Read-only saga view. TTL is advisory and never releases anything."""
    now = utcnow()
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, sku_id, spec, quantity, reserved_by, reserved_at"
            " FROM inventory_records WHERE reserved_by IS NOT NULL"
            " ORDER BY reserved_at, id"
        ).fetchall()
    finally:
        conn.close()

    reservations: list[ReservationView] = []
    for row in rows:
        reserved_at = parse_dt(row["reserved_at"]) if row["reserved_at"] else now
        age_seconds = max(0, int((now - reserved_at).total_seconds()))
        reservations.append(
            ReservationView(
                inventory_record_id=row["id"],
                sku_id=row["sku_id"],
                spec=row["spec"],
                quantity=row["quantity"],
                approval_id=row["reserved_by"],
                reserved_at=reserved_at,
                age_seconds=age_seconds,
                expires_at=reserved_at + timedelta(seconds=ttl_seconds),
            )
        )
    return ListReservationsOutput(reservations=reservations, checked_at=now)
