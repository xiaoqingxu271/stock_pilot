"""Read-only restart reconciliation.

A process can die between "a human decided" and "the graph resumed", or
between "the mall committed" and "the task record was updated". This module
reports those non-terminal tasks so the operator can see them explicitly;
it never writes to any store.
"""

from __future__ import annotations

from stock_pilot.db import connect
from stock_pilot.enums import (
    ApprovalStatus,
    ListingStatus,
    RecoveryKind,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.models import RecoveryIssue, from_cents

_WATCHED_STATUSES = (
    TaskStatus.RECEIVED.value,
    TaskStatus.AWAITING_APPROVAL.value,
    TaskStatus.PUBLISHED.value,
)


def scan(db_path: str) -> list[RecoveryIssue]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT t.id AS task_id, t.sku_id, t.spec, t.status AS task_status,"
            " t.target_price_cents, t.listing_id, t.approval_id,"
            " a.status AS approval_status, s.status AS staging_status,"
            " l.status AS listing_status, l.price_cents AS listing_price_cents"
            " FROM tasks t"
            " LEFT JOIN approvals a ON a.id = t.approval_id"
            " LEFT JOIN staging s ON s.approval_id = t.approval_id"
            " LEFT JOIN listings l ON l.id = t.listing_id"
            " WHERE t.status IN (?, ?, ?)"
            " ORDER BY t.created_at",
            _WATCHED_STATUSES,
        ).fetchall()
        orphan_rows = conn.execute(
            "SELECT s.id AS staging_id, s.listing_id, s.approval_id,"
            " s.status AS staging_status, a.status AS approval_status,"
            " tk.id AS task_id, tk.sku_id, tk.spec, tk.status AS task_status,"
            " tk.target_price_cents, l.status AS listing_status,"
            " l.price_cents AS listing_price_cents"
            " FROM staging s"
            " JOIN approvals a ON a.id = s.approval_id"
            " LEFT JOIN ("
            "   SELECT approval_id, MIN(id) AS id FROM tasks"
            "   WHERE approval_id IS NOT NULL GROUP BY approval_id"
            " ) t ON t.approval_id = s.approval_id"
            " LEFT JOIN tasks tk ON tk.id = t.id"
            " LEFT JOIN listings l ON l.id = s.listing_id"
            " WHERE s.status = ? AND a.status IN (?, ?)"
            " ORDER BY s.created_at",
            (
                StagingStatus.PRICE_STAGED.value,
                ApprovalStatus.APPROVED.value,
                ApprovalStatus.ABANDONED.value,
            ),
        ).fetchall()
    finally:
        conn.close()

    issues: list[RecoveryIssue] = []
    reported_tasks: set[str] = set()

    for row in rows:
        task_status = TaskStatus(row["task_status"])
        approval_status = (
            ApprovalStatus(row["approval_status"])
            if row["approval_status"] is not None
            else None
        )
        staging_status = (
            StagingStatus(row["staging_status"])
            if row["staging_status"] is not None
            else None
        )
        finding = _classify(task_status, approval_status, staging_status)
        if finding is None:
            continue
        kind, message = finding
        reported_tasks.add(row["task_id"])
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
                listing_status=(
                    ListingStatus(row["listing_status"])
                    if row["listing_status"] is not None
                    else None
                ),
                listing_price=(
                    from_cents(row["listing_price_cents"])
                    if row["listing_price_cents"] is not None
                    else None
                ),
                approval_id=row["approval_id"],
                approval_status=approval_status,
                staging_status=staging_status,
            )
        )

    # A write timeout can leave a live staging row behind a terminal task.
    # Report it instead of silently keeping an approved proposal alive.
    for row in orphan_rows:
        if row["task_id"] is not None and row["task_id"] in reported_tasks:
            continue
        aborted = ApprovalStatus(row["approval_status"]) is ApprovalStatus.ABANDONED
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
                task_id=row["task_id"],
                sku_id=row["sku_id"],
                spec=row["spec"],
                task_status=(
                    TaskStatus(row["task_status"])
                    if row["task_status"] is not None
                    else None
                ),
                target_price=(
                    from_cents(row["target_price_cents"])
                    if row["target_price_cents"] is not None
                    else None
                ),
                listing_id=row["listing_id"],
                listing_status=(
                    ListingStatus(row["listing_status"])
                    if row["listing_status"] is not None
                    else None
                ),
                listing_price=(
                    from_cents(row["listing_price_cents"])
                    if row["listing_price_cents"] is not None
                    else None
                ),
                approval_id=row["approval_id"],
                approval_status=ApprovalStatus(row["approval_status"]),
                staging_status=StagingStatus(row["staging_status"]),
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
