"""Mall system module: listing lookup, two-phase price staging, atomic commit."""

from __future__ import annotations

import sqlite3

from stock_pilot.db import connect, iso, new_id, parse_dt, transaction, utcnow
from stock_pilot.enums import (
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    PendingStage,
    StagingStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    GetListingInput,
    GetListingOutput,
    ListingCandidate,
    PendingState,
    PublishListingInput,
    PublishListingOutput,
    UpdatePriceInput,
    UpdatePriceOutput,
    from_cents,
    to_cents,
)


def _candidate(
    row: sqlite3.Row, pending: PendingState | None = None
) -> ListingCandidate:
    return ListingCandidate(
        listing_id=row["id"],
        sku_id=row["sku_id"],
        title=row["title"],
        spec=row["spec"],
        price=from_cents(row["price_cents"]),
        status=ListingStatus(row["status"]),
        inventory_record_id=row["inventory_record_id"],
        pending=pending,
    )


def get_listing(db_path: str, request: GetListingInput) -> GetListingOutput:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, sku_id, title, spec, price_cents, status,"
            " inventory_record_id FROM listings WHERE sku_id = ? ORDER BY id",
            (request.sku_id,),
        ).fetchall()
        pendings = {row["id"]: _pending_for_listing(conn, row["id"]) for row in rows}
    finally:
        conn.close()

    if not rows:
        return GetListingOutput(result=ListingResult.NOT_FOUND)
    if len(rows) > 1:
        return GetListingOutput(
            result=ListingResult.AMBIGUOUS,
            candidates=[
                _candidate(row, pendings[row["id"]]) for row in rows
            ],
        )

    return GetListingOutput(
        result=ListingResult.RESOLVED,
        listing=_candidate(rows[0], pendings[rows[0]["id"]]),
        pending=pendings[rows[0]["id"]],
    )


def get_listing_by_id(
    db_path: str, listing_id: str
) -> ListingCandidate | None:
    """Admin-plane lookup by listing id (used by orphan-proposal cleanup)."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, sku_id, title, spec, price_cents, status,"
            " inventory_record_id FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        pending = (
            _pending_for_listing(conn, listing_id) if row is not None else None
        )
    finally:
        conn.close()
    return _candidate(row, pending) if row is not None else None


def _pending_for_listing(
    conn: sqlite3.Connection, listing_id: str
) -> PendingState | None:
    row = conn.execute(
        "SELECT s.new_price_cents, s.original_price_cents, s.updated_at,"
        "       s.approval_id, a.status AS approval_status"
        " FROM staging s JOIN approvals a ON a.id = s.approval_id"
        " WHERE s.listing_id = ? AND s.status = ?"
        " ORDER BY s.created_at DESC LIMIT 1",
        (listing_id, StagingStatus.PRICE_STAGED.value),
    ).fetchone()
    if row is None:
        return None

    approval_status = ApprovalStatus(row["approval_status"])
    stage = (
        PendingStage.PUBLISH_PENDING
        if approval_status is ApprovalStatus.APPROVED
        else PendingStage.PRICE_STAGED
    )
    return PendingState(
        stage=stage,
        approval_id=row["approval_id"],
        approval_status=approval_status,
        staged_price=from_cents(row["new_price_cents"]),
        original_price=from_cents(row["original_price_cents"]),
        updated_at=parse_dt(row["updated_at"]),
    )


def update_price(db_path: str, request: UpdatePriceInput) -> UpdatePriceOutput:
    """Stage a combined price+publish proposal. The listing is not modified."""
    new_cents = to_cents(request.new_price)
    now = iso(utcnow())

    with transaction(db_path) as tx:
        listing = tx.execute(
            "SELECT id, price_cents FROM listings WHERE id = ?",
            (request.listing_id,),
        ).fetchone()
        if listing is None:
            raise ToolFailure(
                ErrorCode.LISTING_NOT_FOUND,
                f"listing {request.listing_id!r} does not exist",
            )

        existing = tx.execute(
            "SELECT id FROM staging WHERE listing_id = ? AND status = ?",
            (request.listing_id, StagingStatus.PRICE_STAGED.value),
        ).fetchone()
        if existing is not None:
            raise ToolFailure(
                ErrorCode.PENDING_APPROVAL_EXISTS,
                f"listing {request.listing_id!r} already has a live price proposal",
            )

        approval_id = new_id("apr")
        staging_id = new_id("stg")
        original_cents = listing["price_cents"]

        # Staging and approval are created in the same transaction: a timeout
        # can never leave one without the other.
        tx.execute(
            "INSERT INTO approvals (id, listing_id, action, new_price_cents,"
            " original_price_cents, proposal_reason, status, created_at)"
            " VALUES (?, ?, 'price_and_publish', ?, ?, ?, ?, ?)",
            (
                approval_id,
                request.listing_id,
                new_cents,
                original_cents,
                request.proposal_reason,
                ApprovalStatus.PENDING.value,
                now,
            ),
        )
        tx.execute(
            "INSERT INTO staging (id, listing_id, approval_id, new_price_cents,"
            " original_price_cents, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                staging_id,
                request.listing_id,
                approval_id,
                new_cents,
                original_cents,
                StagingStatus.PRICE_STAGED.value,
                now,
                now,
            ),
        )

    return UpdatePriceOutput(
        approval_id=approval_id,
        listing_id=request.listing_id,
        new_price=from_cents(new_cents),
        original_price=from_cents(original_cents),
        approval_status=ApprovalStatus.PENDING,
    )


def publish_listing(
    db_path: str,
    request: PublishListingInput,
    *,
    check_stock: bool = True,
) -> PublishListingOutput:
    """Atomically commit the staged price and publish the listing.

    ``check_stock`` is the shared-store defence against a depleted row at
    commit time. In split stores the mall owns no inventory data; the
    orchestrator holds a reservation on the inventory service instead, and
    passes ``check_stock=False`` (see ADR 0005).
    """
    now = iso(utcnow())

    with transaction(db_path) as tx:
        approval = tx.execute(
            "SELECT id, listing_id, status, new_price_cents, original_price_cents"
            " FROM approvals WHERE id = ?",
            (request.approval_id,),
        ).fetchone()
        if approval is None:
            raise ToolFailure(
                ErrorCode.APPROVAL_NOT_FOUND,
                f"approval {request.approval_id!r} does not exist",
            )
        if approval["listing_id"] != request.listing_id:
            raise ToolFailure(
                ErrorCode.APPROVAL_LISTING_MISMATCH,
                f"approval {request.approval_id!r} belongs to another listing",
            )

        approval_status = ApprovalStatus(approval["status"])
        if approval_status is ApprovalStatus.PENDING:
            raise ToolFailure(
                ErrorCode.APPROVAL_NOT_APPROVED,
                f"approval {request.approval_id!r} is still pending",
            )
        if approval_status is ApprovalStatus.REJECTED:
            raise ToolFailure(
                ErrorCode.APPROVAL_REJECTED,
                f"approval {request.approval_id!r} was rejected",
            )
        if approval_status is ApprovalStatus.CONSUMED:
            raise ToolFailure(
                ErrorCode.APPROVAL_ALREADY_CONSUMED,
                f"approval {request.approval_id!r} was already consumed",
            )
        if approval_status is ApprovalStatus.ABANDONED:
            raise ToolFailure(
                ErrorCode.APPROVAL_NOT_APPROVED,
                f"approval {request.approval_id!r} was abandoned by the system",
            )

        staging = tx.execute(
            "SELECT id, new_price_cents, original_price_cents, status"
            " FROM staging WHERE approval_id = ?",
            (request.approval_id,),
        ).fetchone()
        if staging is None or staging["status"] != StagingStatus.PRICE_STAGED.value:
            raise ToolFailure(
                ErrorCode.STAGING_NOT_FOUND,
                f"no live staged price for approval {request.approval_id!r}",
            )

        listing = tx.execute(
            "SELECT id, price_cents, inventory_record_id FROM listings WHERE id = ?",
            (request.listing_id,),
        ).fetchone()
        if listing is None:
            raise ToolFailure(
                ErrorCode.LISTING_NOT_FOUND,
                f"listing {request.listing_id!r} does not exist",
            )
        if listing["price_cents"] != staging["original_price_cents"]:
            raise ToolFailure(
                ErrorCode.LISTING_PRICE_CHANGED,
                "listing price changed after the proposal was staged; refusing to overwrite",
            )

        # Last line of defence against the recheck/commit race: inventory can
        # still change between the agent's read-only recheck and this commit.
        if check_stock:
            inventory = tx.execute(
                "SELECT quantity FROM inventory_records WHERE id = ?",
                (listing["inventory_record_id"],),
            ).fetchone()
            if inventory is None:
                raise ToolFailure(
                    ErrorCode.INVENTORY_NOT_FOUND,
                    "inventory record disappeared before commit",
                )
            if inventory["quantity"] <= 0:
                raise ToolFailure(
                    ErrorCode.INSUFFICIENT_STOCK,
                    "inventory is depleted at commit time",
                )

        tx.execute(
            "UPDATE listings SET price_cents = ?, status = ?, updated_at = ?"
            " WHERE id = ?",
            (
                staging["new_price_cents"],
                ListingStatus.PUBLISHED.value,
                now,
                request.listing_id,
            ),
        )
        tx.execute(
            "UPDATE approvals SET status = ?, consumed_at = ? WHERE id = ?",
            (ApprovalStatus.CONSUMED.value, now, request.approval_id),
        )
        tx.execute(
            "UPDATE staging SET status = ?, updated_at = ? WHERE id = ?",
            (StagingStatus.COMMITTED.value, now, staging["id"]),
        )

    return PublishListingOutput(
        listing_id=request.listing_id,
        approval_id=request.approval_id,
        final_status=ListingStatus.PUBLISHED,
        final_price=from_cents(staging["new_price_cents"]),
        committed_at=parse_dt(now),
    )
