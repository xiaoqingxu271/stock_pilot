"""Read-only audit export spanning the orchestrator and mall stores.

The export is deliberately a projection, not a new source of truth: every row
is read from the same tables the runtime trusts. It never calls a tool, never
opens a write transaction, and never serializes credentials, notification
payload bodies, or free-form delivery errors (which could contain arbitrary
upstream text).

In split layouts tasks/tool events/outbox live in the orchestrator database
and approvals live with the mall. There is no cross-store transaction, so a
concurrent writer may land between the two queries; that is acceptable for an
operator snapshot and is stated in the response model.
"""

from __future__ import annotations

from datetime import datetime, timezone

from stock_pilot import approval, tasks
from stock_pilot.audit import tool_event_from_row
from stock_pilot.db import connect, iso, parse_dt, utcnow
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import ToolFailure
from stock_pilot.models import AuditExport, AuditOutboxEvent

MAX_LIMIT = 5000


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _where(
    column: str, since: datetime | None, until: datetime | None
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(iso(since))
    if until is not None:
        clauses.append(f"{column} < ?")
        params.append(iso(until))
    return (f" WHERE {' AND '.join(clauses)}" if clauses else "", params)


def export_audit(
    orchestrator_db: str,
    approval_db: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
) -> AuditExport:
    """Return a bounded, read-only snapshot ordered oldest first.

    ``since`` is inclusive and ``until`` exclusive. ``limit`` applies to each
    collection independently, so a caller that needs the complete history of a
    large deployment should page by time window.
    """
    if limit <= 0 or limit > MAX_LIMIT:
        raise ToolFailure(
            ErrorCode.VALIDATION_ERROR,
            f"limit must be between 1 and {MAX_LIMIT}",
        )
    since = _as_utc(since)
    until = _as_utc(until)
    if since is not None and until is not None and since > until:
        raise ToolFailure(
            ErrorCode.VALIDATION_ERROR,
            "since must not be after until",
        )

    task_where, task_params = _where("created_at", since, until)
    event_where, event_params = _where("created_at", since, until)
    outbox_where, outbox_params = _where("created_at", since, until)

    conn = connect(orchestrator_db)
    try:
        task_rows = conn.execute(
            f"SELECT * FROM tasks{task_where} ORDER BY created_at, id LIMIT ?",
            (*task_params, limit),
        ).fetchall()
        event_rows = conn.execute(
            f"SELECT * FROM tool_events{event_where}"
            " ORDER BY created_at, id LIMIT ?",
            (*event_params, limit),
        ).fetchall()
        outbox_rows = conn.execute(
            "SELECT id, dedupe_key, topic, task_id, created_at, delivered_at,"
            f" dead_lettered_at, attempts FROM outbox_events{outbox_where}"
            " ORDER BY created_at, id LIMIT ?",
            (*outbox_params, limit),
        ).fetchall()
    finally:
        conn.close()

    # Approvals live with the mall; this is a separate read-only connection.
    approval_where, approval_params = _where("created_at", since, until)
    conn = connect(approval_db)
    try:
        approval_rows = conn.execute(
            f"SELECT * FROM approvals{approval_where}"
            " ORDER BY created_at, id LIMIT ?",
            (*approval_params, limit),
        ).fetchall()
    finally:
        conn.close()

    return AuditExport(
        generated_at=utcnow(),
        since=since,
        until=until,
        tasks=[tasks.task_from_row(row) for row in task_rows],
        approvals=[approval.approval_from_row(row) for row in approval_rows],
        tool_events=[tool_event_from_row(row) for row in event_rows],
        outbox=[
            AuditOutboxEvent(
                event_id=row["id"],
                dedupe_key=row["dedupe_key"],
                topic=row["topic"],
                task_id=row["task_id"],
                created_at=parse_dt(row["created_at"]),
                delivered_at=(
                    parse_dt(row["delivered_at"]) if row["delivered_at"] else None
                ),
                dead_lettered_at=(
                    parse_dt(row["dead_lettered_at"])
                    if row["dead_lettered_at"]
                    else None
                ),
                attempts=row["attempts"],
            )
            for row in outbox_rows
        ],
    )
