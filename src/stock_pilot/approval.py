"""Approval queue module (human-facing, not an MCP tool)."""

from __future__ import annotations

import sqlite3

from stock_pilot.db import connect, iso, parse_dt, transaction, utcnow
from stock_pilot.enums import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    StagingStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    ApprovalQueueItem,
    ApprovalView,
    StagingView,
    from_cents,
)


def _approval_view(row: sqlite3.Row) -> ApprovalView:
    return ApprovalView(
        approval_id=row["id"],
        listing_id=row["listing_id"],
        action=ApprovalAction(row["action"]),
        new_price=from_cents(row["new_price_cents"]),
        original_price=from_cents(row["original_price_cents"]),
        proposal_reason=row["proposal_reason"],
        status=ApprovalStatus(row["status"]),
        decision_note=row["decision_note"],
        decided_by=row["decided_by"],
        created_at=parse_dt(row["created_at"]),
        decided_at=parse_dt(row["decided_at"]) if row["decided_at"] else None,
        consumed_at=parse_dt(row["consumed_at"]) if row["consumed_at"] else None,
    )


def get_approval(db_path: str, approval_id: str) -> ApprovalView | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    return _approval_view(row) if row is not None else None


def get_staging_for_approval(db_path: str, approval_id: str) -> StagingView | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM staging WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return StagingView(
        staging_id=row["id"],
        listing_id=row["listing_id"],
        approval_id=row["approval_id"],
        new_price=from_cents(row["new_price_cents"]),
        original_price=from_cents(row["original_price_cents"]),
        status=StagingStatus(row["status"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def list_approvals(
    db_path: str, status: ApprovalStatus | None = None
) -> list[ApprovalView]:
    conn = connect(db_path)
    try:
        if status is None:
            rows = conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
    finally:
        conn.close()
    return [_approval_view(row) for row in rows]


def list_queue(
    db_path: str,
    status: ApprovalStatus | None = ApprovalStatus.PENDING,
    limit: int = 100,
) -> list[ApprovalQueueItem]:
    """Approval entries joined with listing and task context for the UI."""
    sql = (
        "SELECT a.id AS approval_id, t.id AS task_id, a.listing_id,"
        " l.sku_id, l.title, l.spec, l.price_cents AS current_price_cents,"
        " a.original_price_cents, a.new_price_cents, a.proposal_reason,"
        " a.status, a.decision_note, a.decided_by, a.created_at, a.decided_at"
        " FROM approvals a"
        " JOIN listings l ON l.id = a.listing_id"
        " LEFT JOIN ("
        "   SELECT approval_id, MIN(id) AS id FROM tasks"
        "   WHERE approval_id IS NOT NULL GROUP BY approval_id"
        " ) t ON t.approval_id = a.id"
    )
    params: list[object] = []
    if status is not None:
        sql += " WHERE a.status = ?"
        params.append(status.value)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    params.append(limit)

    conn = connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        ApprovalQueueItem(
            approval_id=row["approval_id"],
            task_id=row["task_id"],
            listing_id=row["listing_id"],
            sku_id=row["sku_id"],
            title=row["title"],
            spec=row["spec"],
            current_price=from_cents(row["current_price_cents"]),
            original_price=from_cents(row["original_price_cents"]),
            new_price=from_cents(row["new_price_cents"]),
            proposal_reason=row["proposal_reason"],
            status=ApprovalStatus(row["status"]),
            decision_note=row["decision_note"],
            decided_by=row["decided_by"],
            created_at=parse_dt(row["created_at"]),
            decided_at=(
                parse_dt(row["decided_at"]) if row["decided_at"] else None
            ),
        )
        for row in rows
    ]


def decide_approval(
    db_path: str,
    approval_id: str,
    decision: ApprovalDecision,
    note: str | None = None,
    decided_by: str | None = None,
) -> ApprovalView:
    """Record a human decision.

    Approval never writes to the mall. Rejection additionally discards the
    staged proposal so no "price changed but not published" state can exist.
    """
    now = iso(utcnow())
    operator = decided_by.strip() if decided_by is not None else None
    if operator == "":
        operator = None
    if operator is not None and len(operator) > 64:
        raise ToolFailure(
            ErrorCode.VALIDATION_ERROR, "decided_by must be at most 64 characters"
        )

    with transaction(db_path) as tx:
        row = tx.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ToolFailure(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"approval {approval_id!r} does not exist",
            )
        if ApprovalStatus(row["status"]) is not ApprovalStatus.PENDING:
            raise ToolFailure(
                ErrorCode.APPROVAL_ALREADY_DECIDED,
                f"approval {approval_id!r} is already {row['status']}",
            )

        if decision is ApprovalDecision.APPROVE:
            tx.execute(
                "UPDATE approvals SET status = ?, decision_note = ?, decided_by = ?,"
                " decided_at = ? WHERE id = ?",
                (ApprovalStatus.APPROVED.value, note, operator, now, approval_id),
            )
        else:
            tx.execute(
                "UPDATE approvals SET status = ?, decision_note = ?, decided_by = ?,"
                " decided_at = ? WHERE id = ?",
                (ApprovalStatus.REJECTED.value, note, operator, now, approval_id),
            )
            tx.execute(
                "UPDATE staging SET status = ?, updated_at = ?"
                " WHERE approval_id = ? AND status = ?",
                (
                    StagingStatus.DISCARDED.value,
                    now,
                    approval_id,
                    StagingStatus.PRICE_STAGED.value,
                ),
            )

        updated = tx.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()

    return _approval_view(updated)
