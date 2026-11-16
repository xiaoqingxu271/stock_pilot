"""Read-only restart reconciliation.

A process can die between "a human decided" and "the graph resumed", or
between "the mall committed" and "the task record was updated". This module
reports those non-terminal tasks so the operator can see them explicitly;
it never writes to any store.
"""

from __future__ import annotations

from stock_pilot.db import connect, parse_dt, utcnow
from stock_pilot.enums import (
    ApprovalStatus,
    ListingStatus,
    RecoveryKind,
    ReservationGcAction,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.models import RecoveryIssue, from_cents
from stock_pilot.reservations import disposition

_WATCHED_STATUSES = (
    TaskStatus.RECEIVED.value,
    TaskStatus.AWAITING_APPROVAL.value,
    TaskStatus.PUBLISHED.value,
)


def scan(
    db_path: str,
    *,
    mall_db_path: str | None = None,
    inventory_db_path: str | None = None,
    reservation_ttl_seconds: int = 900,
) -> list[RecoveryIssue]:
    """Join task state with mall and inventory state across store files."""
    mall_path = mall_db_path or db_path
    inventory_path = inventory_db_path or db_path

    task_conn = connect(db_path)
    try:
        rows = task_conn.execute(
            "SELECT id AS task_id, sku_id, spec, status AS task_status,"
            " target_price_cents, listing_id, approval_id"
            " FROM tasks WHERE status IN (?, ?, ?) ORDER BY created_at",
            _WATCHED_STATUSES,
        ).fetchall()
        # First task per approval, mirroring the old MIN(id) join, so an
        # orphaned proposal can still be attributed to its task.
        task_by_approval = {}
        for task in task_conn.execute(
            "SELECT id AS task_id, sku_id, spec, status AS task_status,"
            " target_price_cents, approval_id FROM tasks"
            " WHERE approval_id IS NOT NULL ORDER BY id"
        ).fetchall():
            task_by_approval.setdefault(task["approval_id"], task)
    finally:
        task_conn.close()

    mall_conn = connect(mall_path)
    try:
        approvals: dict[str, ApprovalStatus] = {
            row["id"]: ApprovalStatus(row["status"])
            for row in mall_conn.execute(
                "SELECT id, status FROM approvals"
            ).fetchall()
        }
        staging: dict[str, StagingStatus] = {
            row["approval_id"]: StagingStatus(row["status"])
            for row in mall_conn.execute(
                "SELECT approval_id, status FROM staging"
            ).fetchall()
        }
        listings: dict[str, tuple[ListingStatus, int]] = {
            row["id"]: (ListingStatus(row["status"]), row["price_cents"])
            for row in mall_conn.execute(
                "SELECT id, status, price_cents FROM listings"
            ).fetchall()
        }
        orphan_rows = mall_conn.execute(
            "SELECT s.listing_id, s.approval_id,"
            " s.status AS staging_status, a.status AS approval_status"
            " FROM staging s JOIN approvals a ON a.id = s.approval_id"
            " WHERE s.status = ? AND a.status IN (?, ?)"
            " ORDER BY s.created_at",
            (
                StagingStatus.PRICE_STAGED.value,
                ApprovalStatus.APPROVED.value,
                ApprovalStatus.ABANDONED.value,
            ),
        ).fetchall()
    finally:
        mall_conn.close()

    inventory_conn = connect(inventory_path)
    try:
        reservation_rows = inventory_conn.execute(
            "SELECT id, sku_id, spec, reserved_by, reserved_at"
            " FROM inventory_records WHERE reserved_by IS NOT NULL"
            " ORDER BY reserved_at, id"
        ).fetchall()
    finally:
        inventory_conn.close()

    issues: list[RecoveryIssue] = []
    reported_tasks: set[str] = set()

    for row in rows:
        task_status = TaskStatus(row["task_status"])
        approval_status = approvals.get(row["approval_id"] or "")
        staging_status = staging.get(row["approval_id"] or "")
        finding = _classify(task_status, approval_status, staging_status)
        if finding is None:
            continue
        kind, message = finding
        reported_tasks.add(row["task_id"])
        listing = listings.get(row["listing_id"] or "")
        issues.append(
            RecoveryIssue(
                kind=kind,
                message=message,
                task_id=row["task_id"],
                sku_id=row["sku_id"],
                spec=row["spec"],
                task_status=task_status,
                target_price=from_cents(row["target_price_cents"]),
                listing_id=row["listing_id"],
                listing_status=listing[0] if listing else None,
                listing_price=from_cents(listing[1]) if listing else None,
                approval_id=row["approval_id"],
                approval_status=approval_status,
                staging_status=staging_status,
            )
        )

    # A write timeout can leave a live staging row behind a terminal task.
    # Report it instead of silently keeping an approved proposal alive.
    for row in orphan_rows:
        task = task_by_approval.get(row["approval_id"])
        task_id = task["task_id"] if task is not None else None
        if task_id is not None and task_id in reported_tasks:
            continue
        aborted = ApprovalStatus(row["approval_status"]) is ApprovalStatus.ABANDONED
        listing = listings.get(row["listing_id"] or "")
        issues.append(
            RecoveryIssue(
                kind=RecoveryKind.STAGING_ORPHANED,
                message=(
                    "审批已撤回但 staging 仍为 price_staged，需人工清理。"
                    if aborted
                    else (
                        "审批已通过但 staging 未提交，所属任务已不在可自动恢复状态；"
                        "需人工核查或重新提交。"
                    )
                ),
                task_id=task_id,
                sku_id=task["sku_id"] if task is not None else None,
                spec=task["spec"] if task is not None else None,
                task_status=(
                    TaskStatus(task["task_status"]) if task is not None else None
                ),
                target_price=(
                    from_cents(task["target_price_cents"])
                    if task is not None
                    else None
                ),
                listing_id=row["listing_id"],
                listing_status=listing[0] if listing else None,
                listing_price=from_cents(listing[1]) if listing else None,
                approval_id=row["approval_id"],
                approval_status=ApprovalStatus(row["approval_status"]),
                staging_status=StagingStatus(row["staging_status"]),
            )
        )

    # A crash between reserve and release leaves stock held forever. Report
    # only reservations whose approval can no longer publish; an approved
    # proposal that is still active is a normal saga step, never a leak.
    now = utcnow()
    for row in reservation_rows:
        reserved_at = (
            parse_dt(row["reserved_at"]) if row["reserved_at"] else now
        )
        age_seconds = max(0, int((now - reserved_at).total_seconds()))
        approval_status = approvals.get(row["reserved_by"])
        task = task_by_approval.get(row["reserved_by"])
        task_status = (
            TaskStatus(task["task_status"]) if task is not None else None
        )
        action, message = disposition(
            approval_status,
            task_status,
            age_seconds,
            reservation_ttl_seconds,
        )
        if action is ReservationGcAction.RETAINED:
            continue
        kind = (
            RecoveryKind.RESERVATION_ORPHANED
            if action is ReservationGcAction.RELEASED
            else RecoveryKind.RESERVATION_STALE
        )
        task_id = task["task_id"] if task is not None else None
        issues.append(
            RecoveryIssue(
                kind=kind,
                message=f"库存预留在任务结束后仍未释放：{message}",
                task_id=task_id,
                sku_id=row["sku_id"],
                spec=row["spec"],
                task_status=task_status,
                target_price=(
                    from_cents(task["target_price_cents"])
                    if task is not None
                    else None
                ),
                approval_id=row["reserved_by"],
                approval_status=approval_status,
                inventory_record_id=row["id"],
                reserved_at=reserved_at,
            )
        )

    return issues


def _classify(
    task_status: TaskStatus,
    approval_status: ApprovalStatus | None,
    staging_status: StagingStatus | None,
) -> tuple[RecoveryKind, str] | None:
    if task_status is TaskStatus.RECEIVED:
        return (
            RecoveryKind.INTERRUPTED_BEFORE_APPROVAL,
            "任务在进入审批前中断；执行链可能停在检查阶段，需人工核对。",
        )

    if task_status is TaskStatus.PUBLISHED:
        return (
            RecoveryKind.PUBLISHED_VERIFY_PENDING,
            "商城提交已完成但最终核对未写回；需继续只读核对。",
        )

    if approval_status is None:
        return (
            RecoveryKind.APPROVAL_MISSING,
            "任务处于待审批但没有关联审批记录，需人工核查。",
        )

    if approval_status is ApprovalStatus.PENDING:
        return None  # normal: waiting for a human

    if approval_status is ApprovalStatus.APPROVED:
        if staging_status is StagingStatus.PRICE_STAGED:
            return (
                RecoveryKind.APPROVED_NOT_PUBLISHED,
                "审批已通过但尚未提交到商城；可执行恢复，由 publish_listing 原子提交。",
            )
        return (
            RecoveryKind.APPROVED_WITHOUT_STAGING,
            "审批已通过但没有有效的 price_staged 提案，需人工核查。",
        )

    if approval_status is ApprovalStatus.REJECTED:
        return (
            RecoveryKind.REJECTED_NOT_CLOSED,
            "审批已拒绝但任务尚未关闭；恢复只会回写 rolled_back，不触碰商城。",
        )

    if approval_status is ApprovalStatus.ABANDONED:
        return (
            RecoveryKind.ABANDONED_NOT_CLOSED,
            "提案已因发布前库存复核被系统撤回；恢复只会回写 rolled_back，不触碰商城。",
        )

    return (
        RecoveryKind.PUBLISHED_VERIFY_PENDING,
        "审批已消费但任务未完成核对；恢复会只读回读并核对最终状态。",
    )
