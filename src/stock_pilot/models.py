"""Pydantic contracts for every MCP tool and the FastAPI surface."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from stock_pilot.enums import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    PendingStage,
    RecoveryKind,
    StagingStatus,
    TaskStatus,
    ToolOutcome,
)

Money = Annotated[
    Decimal,
    Field(gt=0, le=Decimal("999999.99"), decimal_places=2, max_digits=10),
]
ShortText = Annotated[str, Field(min_length=1, max_length=64)]
SpecText = Annotated[str, Field(min_length=1, max_length=128)]


def to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """Every tool returns an explicit outcome instead of leaking exceptions."""

    ok: bool = True
    error_code: ErrorCode | None = None
    message: str | None = None

    @classmethod
    def fail(cls, code: ErrorCode, message: str) -> ToolOutput:
        return cls(ok=False, error_code=code, message=message)


# ---------------------------------------------------------------------------
# Tool inputs
# ---------------------------------------------------------------------------


class CheckInventoryInput(StrictModel):
    sku_id: ShortText
    spec: SpecText


class GetListingInput(StrictModel):
    sku_id: ShortText


class UpdatePriceInput(StrictModel):
    listing_id: ShortText
    new_price: Money
    proposal_reason: Annotated[str, Field(min_length=1, max_length=500)]


class PublishListingInput(StrictModel):
    listing_id: ShortText
    approval_id: ShortText


# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------


class PendingState(BaseModel):
    """Read-only reconciliation view of a live price proposal."""

    stage: PendingStage
    approval_id: str
    approval_status: ApprovalStatus
    staged_price: Money
    original_price: Money
    updated_at: datetime


class ListingCandidate(BaseModel):
    listing_id: str
    sku_id: str
    title: str
    spec: str
    price: Money
    status: ListingStatus
    inventory_record_id: str
    # Present on ambiguous candidates too, so a human-confirmed listing can
    # still be reconciled after a write timeout.
    pending: PendingState | None = None


class StagingView(BaseModel):
    staging_id: str
    listing_id: str
    approval_id: str
    new_price: Money
    original_price: Money
    status: StagingStatus
    created_at: datetime
    updated_at: datetime


class ApprovalView(BaseModel):
    approval_id: str
    listing_id: str
    action: ApprovalAction
    new_price: Money
    original_price: Money
    proposal_reason: str
    status: ApprovalStatus
    decision_note: str | None = None
    decided_by: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    abandoned_at: datetime | None = None
    abandon_reason: str | None = None
    abandoned_by: str | None = None


class ApprovalQueueItem(BaseModel):
    """Approval queue entry enriched with listing/task context for the UI."""

    approval_id: str
    task_id: str | None = None
    listing_id: str
    sku_id: str
    title: str
    spec: str
    current_price: Money
    original_price: Money
    new_price: Money
    proposal_reason: str
    status: ApprovalStatus
    decision_note: str | None = None
    decided_by: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    abandoned_at: datetime | None = None
    abandon_reason: str | None = None
    abandoned_by: str | None = None


class VerificationResult(BaseModel):
    listing_id: str
    expected_price: Money
    actual_price: Money | None = None
    expected_status: ListingStatus
    actual_status: ListingStatus | None = None
    matched: bool = False
    checked_at: datetime


# ---------------------------------------------------------------------------
# Tool outputs
# ---------------------------------------------------------------------------


class CheckInventoryOutput(ToolOutput):
    sku_id: str | None = None
    spec: str | None = None
    quantity: int | None = None
    timestamp: datetime | None = None
    inventory_record_id: str | None = None


class GetListingOutput(ToolOutput):
    result: ListingResult | None = None
    listing: ListingCandidate | None = None
    candidates: list[ListingCandidate] = Field(default_factory=list)
    pending: PendingState | None = None


class UpdatePriceOutput(ToolOutput):
    approval_id: str | None = None
    listing_id: str | None = None
    new_price: Money | None = None
    original_price: Money | None = None
    approval_status: ApprovalStatus | None = None


class PublishListingOutput(ToolOutput):
    listing_id: str | None = None
    approval_id: str | None = None
    final_status: ListingStatus | None = None
    final_price: Money | None = None
    committed_at: datetime | None = None


# ---------------------------------------------------------------------------
# FastAPI contracts
# ---------------------------------------------------------------------------


class CreateTaskRequest(StrictModel):
    sku_id: ShortText
    spec: SpecText
    target_price: Money
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ApprovalDecisionRequest(StrictModel):
    decision: ApprovalDecision
    note: str | None = Field(default=None, max_length=500)
    decided_by: str | None = Field(default=None, max_length=64)


class MappingConfirmRequest(StrictModel):
    listing_id: ShortText


class AbandonApprovalRequest(StrictModel):
    reason: str = Field(default="人工清理遗留提案", min_length=1, max_length=500)


class TaskVerification(BaseModel):
    result: VerificationResult
    verified_at: datetime


class TaskView(BaseModel):
    task_id: str
    sku_id: str
    spec: str
    target_price: Money
    reason: str
    idempotency_key: str | None = None
    status: TaskStatus
    listing_id: str | None = None
    approval_id: str | None = None
    original_price: Money | None = None
    final_price: Money | None = None
    candidates: list[ListingCandidate] = Field(default_factory=list)
    verification: TaskVerification | None = None
    error_code: ErrorCode | None = None
    created_at: datetime
    updated_at: datetime


class ToolEvent(BaseModel):
    """One attempt of one tool call, correlated with a task when known."""

    event_id: int
    task_id: str | None = None
    tool: str
    attempt: int
    outcome: ToolOutcome
    error_code: ErrorCode | None = None
    latency_ms: int
    created_at: datetime


class HealthStatus(BaseModel):
    """Readiness snapshot; degraded means writes must not be accepted."""

    status: Literal["ok", "degraded"]
    database: bool
    tool_client: bool
    pending_approvals: int
    recovery_issues: int
    checked_at: datetime


class RecoveryIssue(BaseModel):
    """Read-only finding from the startup/restart reconciliation scan."""

    kind: RecoveryKind
    message: str
    task_id: str | None = None
    sku_id: str | None = None
    spec: str | None = None
    task_status: TaskStatus | None = None
    target_price: Money | None = None
    listing_id: str | None = None
    listing_status: ListingStatus | None = None
    listing_price: Money | None = None
    approval_id: str | None = None
    approval_status: ApprovalStatus | None = None
    staging_status: StagingStatus | None = None
