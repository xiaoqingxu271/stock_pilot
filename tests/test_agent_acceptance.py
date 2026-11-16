"""End-to-end acceptance cases at the LangGraph orchestration layer."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import count_rows, fetch_one

from stock_pilot import approval
from stock_pilot.agent import (
    build_graph,
    confirm_mapping_and_resume,
    decide_and_resume,
    start_task,
)
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    ListingStatus,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    CreateTaskRequest,
    PublishListingOutput,
    UpdatePriceOutput,
)
from stock_pilot.tools import StockPilotTools


def request(
    sku: str = "SKU-TSHIRT-001", spec: str = "白色-M", price: str = "59.00"
) -> CreateTaskRequest:
    return CreateTaskRequest(
        sku_id=sku,
        spec=spec,
        target_price=Decimal(price),
        reason="库存驱动调价",
    )


class _Proxy:
    """Delegates to a real tool facade while overriding one write method."""

    def __init__(self, inner: StockPilotTools) -> None:
        self._inner = inner
        self.settings = inner.settings

    def __getattr__(self, name):
        return getattr(self._inner, name)


class UpdateTimeoutAfterCommit(_Proxy):
    def update_price(self, request):
        real = self._inner.update_price(request)
        assert real.ok, real
        return UpdatePriceOutput.fail(ErrorCode.TOOL_TIMEOUT, "simulated timeout")


class PublishTimeout(_Proxy):
    def __init__(self, inner: StockPilotTools, *, commit: bool) -> None:
        super().__init__(inner)
        self.commit = commit
        self.calls = 0

    def publish_listing(self, request):
        self.calls += 1
        if self.commit:
            real = self._inner.publish_listing(request)
            assert real.ok, real
        return PublishListingOutput.fail(ErrorCode.TOOL_TIMEOUT, "simulated timeout")


# ---------------------------------------------------------------------------
# Case 1: happy path
# ---------------------------------------------------------------------------


def test_happy_path_verified(tools, graph, settings: Settings):
    task = start_task(tools, graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert task.approval_id is not None
    assert task.listing_id == "LST-001"
    assert task.original_price == Decimal("79.00")

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    done = decide_and_resume(
        tools, graph, task.approval_id, ApprovalDecision.APPROVE, "同意"
    )
    assert done.status is TaskStatus.VERIFIED
    assert done.final_price == Decimal("59.00")
    assert done.verification is not None
    assert done.verification.result.matched
    assert done.verification.result.actual_price == Decimal("59.00")
    assert done.verification.result.actual_status is ListingStatus.PUBLISHED

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 5900
    assert row["status"] == "published"

    record = approval.get_approval(settings.db_path, task.approval_id)
    assert record is not None
    assert record.status is ApprovalStatus.CONSUMED


# ---------------------------------------------------------------------------
# Case 2: insufficient stock
# ---------------------------------------------------------------------------


def test_insufficient_stock_aborts_before_any_write(tools, graph, settings: Settings):
    task = start_task(
        tools, graph, request("SKU-HOODIE-003", "黑色-L", "99.00")
    )
    assert task.status is TaskStatus.ABORTED_NO_STOCK
    assert task.error_code is ErrorCode.INSUFFICIENT_STOCK
    assert task.approval_id is None
    assert count_rows(settings, "staging") == 0
    assert count_rows(settings, "approvals") == 0

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-004'"
    )
    assert row["price_cents"] == 19900
    assert row["status"] == "draft"


# ---------------------------------------------------------------------------
# Case 3: ambiguous mapping
# ---------------------------------------------------------------------------


def test_ambiguous_mapping_suspends_with_candidates(tools, graph, settings: Settings):
    task = start_task(
        tools, graph, request("SKU-AMBIG-002", "白色-M", "59.00")
    )
    assert task.status is TaskStatus.SUSPENDED_AMBIGUOUS
    assert len(task.candidates) == 2
    assert task.approval_id is None
    assert count_rows(settings, "staging") == 0
    assert count_rows(settings, "approvals") == 0

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-002'"
    )
    assert row["price_cents"] == 8900
    assert row["status"] == "draft"


def test_human_mapping_choice_resumes_to_approval(tools, graph, settings: Settings):
    task = start_task(
        tools, graph, request("SKU-AMBIG-002", "白色-M", "59.00")
    )
    resumed = confirm_mapping_and_resume(tools, graph, task.task_id, "LST-002")
    assert resumed.status is TaskStatus.AWAITING_APPROVAL
    assert resumed.listing_id == "LST-002"
    assert resumed.approval_id is not None

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-002'"
    )
    assert row["price_cents"] == 8900
    assert row["status"] == "draft"

    done = decide_and_resume(
        tools, graph, resumed.approval_id, ApprovalDecision.APPROVE
    )
    assert done.status is TaskStatus.VERIFIED
    assert done.final_price == Decimal("59.00")


def test_mapping_choice_must_match_spec_and_inventory(tools, graph, settings: Settings):
    task = start_task(
        tools, graph, request("SKU-AMBIG-002", "白色-M", "59.00")
    )
    # Valid candidate id, but it is the 白色-L variant for a 白色-M task.
    aborted = confirm_mapping_and_resume(tools, graph, task.task_id, "LST-003")
    assert aborted.status is TaskStatus.ABORTED_FAILED
    assert aborted.error_code is ErrorCode.MAPPING_MISMATCH
    assert count_rows(settings, "staging") == 0
    assert count_rows(settings, "approvals") == 0


def test_mapping_choice_must_be_a_candidate(tools, graph):
    task = start_task(
        tools, graph, request("SKU-AMBIG-002", "白色-M", "59.00")
    )
    with pytest.raises(ToolFailure) as excinfo:
        confirm_mapping_and_resume(tools, graph, task.task_id, "LST-001")
    assert excinfo.value.code is ErrorCode.MAPPING_MISMATCH


# ---------------------------------------------------------------------------
# Case 4: approval rejection
# ---------------------------------------------------------------------------


def test_rejection_rolls_back_without_dirty_state(tools, graph, settings: Settings):
    task = start_task(tools, graph, request())
    assert task.status is TaskStatus.AWAITING_APPROVAL

    done = decide_and_resume(
        tools, graph, task.approval_id, ApprovalDecision.REJECT, "价格过低"
    )
    assert done.status is TaskStatus.ROLLED_BACK
    assert done.error_code is ErrorCode.APPROVAL_REJECTED

    staging = approval.get_staging_for_approval(settings.db_path, task.approval_id)
    assert staging is not None
    assert staging.status is StagingStatus.DISCARDED

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    record = approval.get_approval(settings.db_path, task.approval_id)
    assert record.status is ApprovalStatus.REJECTED


# ---------------------------------------------------------------------------
# Write-timeout reconciliation
# ---------------------------------------------------------------------------


def test_update_price_timeout_reconciles_and_continues(tools, checkpointer):
    flaky = UpdateTimeoutAfterCommit(tools)
    graph = build_graph(flaky, checkpointer)

    task = start_task(tools, graph, request())
    # The write actually landed; the read-only reconciliation adopts it.
    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert task.approval_id is not None

    done = decide_and_resume(tools, graph, task.approval_id, ApprovalDecision.APPROVE)
    assert done.status is TaskStatus.VERIFIED


def test_publish_timeout_after_commit_reconciles(tools, checkpointer):
    flaky = PublishTimeout(tools, commit=True)
    graph = build_graph(flaky, checkpointer)

    task = start_task(tools, graph, request())
    done = decide_and_resume(tools, graph, task.approval_id, ApprovalDecision.APPROVE)
    assert flaky.calls == 1
    assert done.status is TaskStatus.VERIFIED
    assert done.verification is not None
    assert done.verification.result.matched


def test_publish_timeout_before_commit_aborts_without_retrying(
    tools, checkpointer, settings: Settings
):
    flaky = PublishTimeout(tools, commit=False)
    graph = build_graph(flaky, checkpointer)

    task = start_task(tools, graph, request())
    done = decide_and_resume(tools, graph, task.approval_id, ApprovalDecision.APPROVE)

    assert flaky.calls == 1  # never retried
    assert done.status is TaskStatus.ABORTED_UNKNOWN_STATE
    assert done.error_code is ErrorCode.TOOL_TIMEOUT

    row = fetch_one(
        settings, "SELECT price_cents, status FROM listings WHERE id = 'LST-001'"
    )
    assert row["price_cents"] == 7900
    assert row["status"] == "draft"

    staging = approval.get_staging_for_approval(settings.db_path, task.approval_id)
    assert staging.status is StagingStatus.PRICE_STAGED  # never committed
