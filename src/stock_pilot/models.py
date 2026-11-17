"""Pydantic contracts for every MCP tool and the FastAPI surface."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

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
    ReservationGcAction,
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


class ReserveInventoryInput(StrictModel):
    """Internal saga step; not part of the four model-facing tools."""

    inventory_record_id: ShortText
    approval_id: ShortText


class ReleaseInventoryInput(StrictModel):
    approval_id: ShortText


class ListReservationsInput(StrictModel):
    """Internal saga read; the inventory server reports its own TTL view."""


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
    submitted_by: str | None = None
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


class ReserveInventoryOutput(ToolOutput):
    inventory_record_id: str | None = None
    approval_id: str | None = None
    quantity: int | None = None
    reserved: bool = False
    reserved_at: datetime | None = None
    # TTL is advisory: the sweep never releases a reservation whose approval
    # can still publish, so an expired timestamp is a report, not permission.
    expires_at: datetime | None = None


class ReleaseInventoryOutput(ToolOutput):
    approval_id: str | None = None
    released: bool = False


class ReservationView(BaseModel):
    """One live reservation, enriched with its age for operator review."""

    inventory_record_id: str
    sku_id: str
    spec: str
    quantity: int
    approval_id: str
    reserved_at: datetime
    age_seconds: int
    expires_at: datetime


class ListReservationsOutput(ToolOutput):
    reservations: list[ReservationView] = Field(default_factory=list)
    checked_at: datetime | None = None


# ---------------------------------------------------------------------------
# FastAPI contracts
# ---------------------------------------------------------------------------


class CreateTaskRequest(StrictModel):
    sku_id: ShortText
    spec: SpecText
    target_price: Money
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class TaskIntakeRequest(StrictModel):
    """Free text handed to the optional extraction layer."""

    text: str = Field(min_length=1, max_length=2000)


class TaskDraft(BaseModel):
    """A proposal from intake. It is never persisted and grants no authority.

    Callers must submit it through ``POST /tasks``, where every inventory,
    mapping, and approval check runs again.
    """

    sku_id: ShortText
    spec: SpecText
    target_price: Money
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    warnings: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    """Read-only deployment capabilities for the console.

    Exposes no secrets: the model name is public configuration, while the API
    key stays in the local environment and is never returned.
    """

    intake_enabled: bool
    llm_model: str | None = None
    db_layout: Literal["shared", "split"]
    auth_enabled: bool


class ApprovalDecisionRequest(StrictModel):
    decision: ApprovalDecision
    note: str | None = Field(default=None, max_length=500)
    decided_by: str | None = Field(default=None, max_length=64)


class MappingConfirmRequest(StrictModel):
    listing_id: ShortText


class AbandonApprovalRequest(StrictModel):
    reason: str = Field(default="人工清理遗留提案", min_length=1, max_length=500)


class ReservationGcItem(BaseModel):
    inventory_record_id: str
    approval_id: str
    sku_id: str | None = None
    spec: str | None = None
    approval_status: ApprovalStatus | None = None
    task_status: TaskStatus | None = None
    age_seconds: int
    action: ReservationGcAction
    reason: str


class ReservationGcResponse(BaseModel):
    checked_at: datetime
    released: int
    retained: int
    stale: int
    failed: int
    items: list[ReservationGcItem] = Field(default_factory=list)


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
    submitted_by: str | None = None
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


class OutboxEventView(BaseModel):
    """Read-only operator view of a notification event."""

    event_id: int
    dedupe_key: str
    topic: str
    task_id: str | None
    payload: dict[str, Any]
    created_at: datetime
    delivered_at: datetime | None
    dead_lettered_at: datetime | None
    attempts: int
    last_error: str | None


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
    inventory_record_id: str | None = None
    reserved_at: datetime | None = None
