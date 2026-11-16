"""Inventory system module (read-only tool)."""

from __future__ import annotations

from stock_pilot.db import connect, parse_dt
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import ToolFailure
from stock_pilot.models import CheckInventoryInput, CheckInventoryOutput


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
