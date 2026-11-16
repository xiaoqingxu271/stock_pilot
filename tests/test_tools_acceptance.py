"""Acceptance cases for the tool layer (CLAUDE.md cases 1-4)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from conftest import count_rows, fetch_one

from stock_pilot import approval
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    PendingStage,
    StagingStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.mcp_server import build_mcp_server
from stock_pilot.models import (
    CheckInventoryInput,
    GetListingInput,
    PublishListingInput,
    UpdatePriceInput,
)
from stock_pilot.tools import StockPilotTools


def check(tools: StockPilotTools, sku: str, spec: str):
    return tools.check_inventory(CheckInventoryInput(sku_id=sku, spec=spec))


def listing(tools: StockPilotTools, sku: str):
    return tools.get_listing(GetListingInput(sku_id=sku))


def stage(tools: StockPilotTools, listing_id: str, price: str):
    return tools.update_price(
        UpdatePriceInput(
            listing_id=listing_id,
            new_price=Decimal(price),
            proposal_reason="库存驱动调价",
        )
    )


def publish(tools: StockPilotTools, listing_id: str, approval_id: str):
    return tools.publish_listing(
        PublishListingInput(listing_id=listing_id, approval_id=approval_id)
    )


# ---------------------------------------------------------------------------
# Case 1: happy path
# ---------------------------------------------------------------------------


def test_happy_path_price_and_publish_verified(tools, settings: Settings):
    inv = check(tools, "SKU-TSHIRT-001", "白色-M")
    assert inv.ok
    assert inv.quantity == 20
    assert inv.inventory_record_id == "INV-001"

    resolved = listing(tools, "SKU-TSHIRT-001")
    assert resolved.ok
    assert resolved.result is ListingResult.RESOLVED
    assert resolved.listing is not None
    assert resolved.listing.listing_id == "LST-001"
    assert resolved.listing.price == Decimal("79.00")
    assert resolved.listing.status is ListingStatus.DRAFT
    assert resolved.listing.inventory_record_id == inv.inventory_record_id
    assert resolved.pending is None

    staged = stage(tools, "LST-001", "59.00")
    assert staged.ok
    assert staged.approval_id
    assert staged.original_price == Decimal("79.00")
    assert staged.new_price == Decimal("59.00")
    assert staged.approval_status is ApprovalStatus.PENDING

    # Two-phase: the public listing is untouched while approval is pending.
    row = fetch_one(settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'")
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    pending = listing(tools, "SKU-TSHIRT-001")
    assert pending.pending is not None
    assert pending.pending.stage is PendingStage.PRICE_STAGED
    assert pending.pending.staged_price == Decimal("59.00")
    assert pending.pending.approval_status is ApprovalStatus.PENDING

    # Publishing before approval is rejected and changes nothing.
    premature = publish(tools, "LST-001", staged.approval_id)
    assert not premature.ok
    assert premature.error_code is ErrorCode.APPROVAL_NOT_APPROVED

    decided = tools.decide_approval(
        staged.approval_id, ApprovalDecision.APPROVE, "同意"
    )
    assert decided.status is ApprovalStatus.APPROVED

    approved_view = listing(tools, "SKU-TSHIRT-001")
    assert approved_view.pending is not None
    assert approved_view.pending.stage is PendingStage.PUBLISH_PENDING

    published = publish(tools, "LST-001", staged.approval_id)
    assert published.ok
    assert published.final_status is ListingStatus.PUBLISHED
    assert published.final_price == Decimal("59.00")

    # Final reconciliation reuses get_listing.
    final = listing(tools, "SKU-TSHIRT-001")
    assert final.result is ListingResult.RESOLVED
    assert final.listing is not None
    assert final.listing.price == Decimal("59.00")
    assert final.listing.status is ListingStatus.PUBLISHED
    assert final.pending is None

    again = publish(tools, "LST-001", staged.approval_id)
    assert not again.ok
    assert again.error_code is ErrorCode.APPROVAL_ALREADY_CONSUMED


# ---------------------------------------------------------------------------
# Case 2: insufficient stock -> zero writes
# ---------------------------------------------------------------------------


def test_insufficient_stock_terminates_with_zero_writes(tools, settings: Settings):
    inv = check(tools, "SKU-HOODIE-003", "黑色-L")
    assert inv.ok
    assert inv.quantity == 0

    # The orchestrator must stop here. Verify nothing entered the write path.
    assert count_rows(settings, "staging") == 0
    assert count_rows(settings, "approvals") == 0
    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-004'"
    )
    assert row["price_cents"] == 19900
    assert row["status"] == "draft"


# ---------------------------------------------------------------------------
# Case 3: ambiguous mapping -> suspend, never guess
# ---------------------------------------------------------------------------


def test_ambiguous_mapping_returns_candidates_and_stops(tools, settings: Settings):
    result = listing(tools, "SKU-AMBIG-002")
    assert result.ok
    assert result.result is ListingResult.AMBIGUOUS
    assert result.listing is None
    assert len(result.candidates) == 2
    assert {c.spec for c in result.candidates} == {"白色-M", "白色-L"}
    assert len({c.listing_id for c in result.candidates}) == 2
    assert {c.inventory_record_id for c in result.candidates} == {"INV-002", "INV-003"}

    assert count_rows(settings, "staging") == 0
    assert count_rows(settings, "approvals") == 0


def test_unknown_sku_is_not_found(tools, settings: Settings):
    result = listing(tools, "SKU-DOES-NOT-EXIST")
    assert result.result is ListingResult.NOT_FOUND
    assert result.listing is None
    assert result.candidates == []
    assert count_rows(settings, "staging") == 0


# ---------------------------------------------------------------------------
# Case 4: approval rejection -> rollback, no dirty intermediate state
# ---------------------------------------------------------------------------


def test_approval_rejection_leaves_no_dirty_state(tools, settings: Settings):
    staged = stage(tools, "LST-001", "59.00")
    assert staged.ok

    decided = tools.decide_approval(
        staged.approval_id, ApprovalDecision.REJECT, "价格过低"
    )
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.decision_note == "价格过低"

    staging = approval.get_staging_for_approval(
        settings.db_path, staged.approval_id
    )
    assert staging is not None
    assert staging.status is StagingStatus.DISCARDED

    # Mall state is exactly what it was before the proposal.
    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    snapshot = listing(tools, "SKU-TSHIRT-001")
    assert snapshot.listing is not None
    assert snapshot.listing.price == Decimal("79.00")
    assert snapshot.pending is None

    refused = publish(tools, "LST-001", staged.approval_id)
    assert not refused.ok
    assert refused.error_code is ErrorCode.APPROVAL_REJECTED

    # A rejected proposal cannot be resurrected.
    with pytest.raises(ToolFailure) as excinfo:
        tools.decide_approval(staged.approval_id, ApprovalDecision.APPROVE, "改主意了")
    assert excinfo.value.code is ErrorCode.APPROVAL_ALREADY_DECIDED


# ---------------------------------------------------------------------------
# Additional guard rails
# ---------------------------------------------------------------------------


def test_publish_without_update_is_refused(tools):
    result = publish(tools, "LST-001", "apr_missing")
    assert not result.ok
    assert result.error_code is ErrorCode.APPROVAL_NOT_FOUND


def test_second_proposal_while_pending_is_refused(tools):
    first = stage(tools, "LST-001", "59.00")
    assert first.ok
    second = stage(tools, "LST-001", "49.00")
    assert not second.ok
    assert second.error_code is ErrorCode.PENDING_APPROVAL_EXISTS


def test_missing_inventory_record_is_structured_error(tools):
    result = check(tools, "SKU-NO-INVENTORY", "默认")
    assert not result.ok
    assert result.error_code is ErrorCode.INVENTORY_NOT_FOUND


def test_mcp_server_exposes_exactly_four_tools(tools):
    server = build_mcp_server(tools)
    registered = asyncio.run(server.list_tools())
    assert {tool.name for tool in registered} == {
        "check_inventory",
        "get_listing",
        "update_price",
        "publish_listing",
    }
