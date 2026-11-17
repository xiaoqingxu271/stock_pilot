"""Durable task records (the agent's audit trail)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from stock_pilot import notifications
from stock_pilot.db import connect, iso, new_id, parse_dt, transaction, utcnow
from stock_pilot.enums import ErrorCode, TaskStatus
from stock_pilot.models import (
    CreateTaskRequest,
    ListingCandidate,
    TaskVerification,
    TaskView,
    from_cents,
    to_cents,
)


def create_task(
    db_path: str,
    request: CreateTaskRequest,
    *,
    idempotency_key: str | None = None,
    submitted_by: str | None = None,
) -> TaskView:
    task_id = new_id("task")
    now = iso(utcnow())
    with transaction(db_path) as tx:
        tx.execute(
            "INSERT INTO tasks (id, sku_id, spec, target_price_cents, reason,"
            " idempotency_key, submitted_by, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                request.sku_id,
                request.spec,
                to_cents(request.target_price),
                request.reason,
                idempotency_key,
                submitted_by,
                TaskStatus.RECEIVED.value,
                now,
                now,
            ),
        )
    task = get_task(db_path, task_id)
    assert task is not None
    return task


def find_task_by_idempotency_key(
    db_path: str, idempotency_key: str
) -> TaskView | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
    finally:
        conn.close()
    return task_from_row(row) if row is not None else None


def get_task(db_path: str, task_id: str) -> TaskView | None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    return task_from_row(row) if row is not None else None


def find_task_by_approval(db_path: str, approval_id: str) -> TaskView | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE approval_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (approval_id,),
        ).fetchone()
    finally:
        conn.close()
    return task_from_row(row) if row is not None else None


def list_tasks(db_path: str, limit: int = 100) -> list[TaskView]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [task_from_row(row) for row in rows]


def update_task(
    db_path: str,
    task_id: str,
    *,
    status: TaskStatus | None = None,
    listing_id: str | None = None,
    approval_id: str | None = None,
    original_price: Decimal | None = None,
    final_price: Decimal | None = None,
    candidates: list[ListingCandidate] | None = None,
    verification: TaskVerification | None = None,
    error_code: ErrorCode | None = None,
) -> TaskView:
    assignments: list[str] = []
    values: list[object] = []

    def set_field(column: str, value: object) -> None:
        assignments.append(f"{column} = ?")
        values.append(value)

    if status is not None:
        set_field("status", status.value)
    if listing_id is not None:
        set_field("listing_id", listing_id)
    if approval_id is not None:
        set_field("approval_id", approval_id)
    if original_price is not None:
        set_field("original_price_cents", to_cents(original_price))
    if final_price is not None:
        set_field("final_price_cents", to_cents(final_price))
    if candidates is not None:
        set_field(
            "candidates_json",
            json.dumps(
                [c.model_dump(mode="json") for c in candidates], ensure_ascii=False
            ),
        )
    if verification is not None:
        set_field(
            "verification_json",
            json.dumps(verification.model_dump(mode="json"), ensure_ascii=False),
        )
    if error_code is not None:
        set_field("error_code", error_code.value)

    set_field("updated_at", iso(utcnow()))
    values.append(task_id)

    with transaction(db_path) as tx:
        tx.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", values
        )
        # Task state and its notification share one transaction: either both
        # commit or neither does. Delivery is a later, at-least-once step.
        notifications.enqueue_terminal(tx, task_id)

    task = get_task(db_path, task_id)
    assert task is not None
    return task


def task_from_row(row: sqlite3.Row) -> TaskView:
    candidates = (
        [ListingCandidate.model_validate(item) for item in json.loads(row["candidates_json"])]
        if row["candidates_json"]
        else []
    )
    verification = (
        TaskVerification.model_validate(json.loads(row["verification_json"]))
        if row["verification_json"]
        else None
    )
    return TaskView(
        task_id=row["id"],
        sku_id=row["sku_id"],
        spec=row["spec"],
        target_price=from_cents(row["target_price_cents"]),
        reason=row["reason"],
        idempotency_key=row["idempotency_key"],
        submitted_by=row["submitted_by"],
        status=TaskStatus(row["status"]),
        listing_id=row["listing_id"],
        approval_id=row["approval_id"],
        original_price=(
            from_cents(row["original_price_cents"])
            if row["original_price_cents"] is not None
            else None
        ),
        final_price=(
            from_cents(row["final_price_cents"])
            if row["final_price_cents"] is not None
            else None
        ),
        candidates=candidates,
        verification=verification,
        error_code=(
            ErrorCode(row["error_code"]) if row["error_code"] is not None else None
        ),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )
