"""Domain enums. Error codes are enums everywhere; no bare strings."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # Input / lookup
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVENTORY_NOT_FOUND = "INVENTORY_NOT_FOUND"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    LISTING_NOT_FOUND = "LISTING_NOT_FOUND"
    MAPPING_AMBIGUOUS = "MAPPING_AMBIGUOUS"
    MAPPING_NOT_FOUND = "MAPPING_NOT_FOUND"
    MAPPING_MISMATCH = "MAPPING_MISMATCH"

    # Write-path guards
    PENDING_APPROVAL_EXISTS = "PENDING_APPROVAL_EXISTS"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_LISTING_MISMATCH = "APPROVAL_LISTING_MISMATCH"
    STAGING_NOT_FOUND = "STAGING_NOT_FOUND"
    LISTING_PRICE_CHANGED = "LISTING_PRICE_CHANGED"
    INVALID_STATE = "INVALID_STATE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # Execution failures
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    STATE_UNKNOWN = "STATE_UNKNOWN"


class ListingResult(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class ListingStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    OFF_SHELF = "off_shelf"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


class ApprovalAction(str, Enum):
    PRICE_AND_PUBLISH = "price_and_publish"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class StagingStatus(str, Enum):
    PRICE_STAGED = "price_staged"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class PendingStage(str, Enum):
    """What a read-only reconciliation can observe in get_listing.pending."""

    PRICE_STAGED = "price_staged"
    PUBLISH_PENDING = "publish_pending"


class RecoveryKind(str, Enum):
    """Why a non-terminal task needs attention after a restart."""

    APPROVED_NOT_PUBLISHED = "approved_not_published"
    REJECTED_NOT_CLOSED = "rejected_not_closed"
    PUBLISHED_VERIFY_PENDING = "published_verify_pending"
    APPROVAL_MISSING = "approval_missing"
    APPROVED_WITHOUT_STAGING = "approved_without_staging"
    INTERRUPTED_BEFORE_APPROVAL = "interrupted_before_approval"


class ToolOutcome(str, Enum):
    OK = "ok"
    BUSINESS_ERROR = "business_error"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    CIRCUIT_OPEN = "circuit_open"
    ERROR = "error"


class TaskStatus(str, Enum):
    RECEIVED = "received"
    AWAITING_APPROVAL = "awaiting_approval"
    SUSPENDED_AMBIGUOUS = "suspended_ambiguous"
    PUBLISHED = "published"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    ABORTED_NO_STOCK = "aborted_no_stock"
    ABORTED_NOT_FOUND = "aborted_not_found"
    ABORTED_UNKNOWN_STATE = "aborted_unknown_state"
    ABORTED_FAILED = "aborted_failed"
    FAILED_VERIFICATION = "failed_verification"
