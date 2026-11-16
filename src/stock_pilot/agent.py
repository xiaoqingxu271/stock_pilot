"""LangGraph state machine for the constrained repricing/publish chain.

Safety properties encoded here:

* stock <= 0 terminates before any write;
* ambiguous mapping pauses the graph and requires a human choice;
* approval rejection ends in ROLLED_BACK with nothing written to the mall;
* a timed-out write gets exactly one read-only reconciliation and then the
  task stops — it never issues another write.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from stock_pilot import audit, tasks
from stock_pilot.clients import ToolClient
from stock_pilot.db import utcnow
from stock_pilot.enums import (
    ApprovalDecision,
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    PendingStage,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    CheckInventoryInput,
    CreateTaskRequest,
    GetListingInput,
    PublishListingInput,
    TaskVerification,
    TaskView,
    UpdatePriceInput,
    VerificationResult,
)

TERMINAL = {
    TaskStatus.VERIFIED,
    TaskStatus.ROLLED_BACK,
    TaskStatus.ABORTED_NO_STOCK,
    TaskStatus.ABORTED_NOT_FOUND,
    TaskStatus.ABORTED_UNKNOWN_STATE,
    TaskStatus.ABORTED_FAILED,
    TaskStatus.FAILED_VERIFICATION,
}


class AgentState(TypedDict, total=False):
    task_id: str
    sku_id: str
    spec: str
    target_price: str  # decimal string, safe for checkpoint serialization
    reason: str
    inventory_record_id: str | None
    listing_id: str | None
    approval_id: str | None
    original_price: str | None
    candidates: list[dict[str, Any]]
    status: str
    error_code: str | None
    approval_outcome: str | None
    verification: dict[str, Any] | None


def _target(state: AgentState) -> Decimal:
    return Decimal(state["target_price"])


def _is_terminal(state: AgentState) -> bool:
    try:
        return TaskStatus(state.get("status", "")) in TERMINAL
    except ValueError:
        return False


def _build_verification(
    listing_id: str, expected_price: Decimal, listing
) -> VerificationResult:
    matched = (
        listing is not None
        and listing.listing_id == listing_id
        and listing.price == expected_price
        and listing.status is ListingStatus.PUBLISHED
    )
    return VerificationResult(
        listing_id=listing_id,
        expected_price=expected_price,
        actual_price=listing.price if listing is not None else None,
        expected_status=ListingStatus.PUBLISHED,
        actual_status=listing.status if listing is not None else None,
        matched=matched,
        checked_at=utcnow(),
    )


def _select_listing(result, listing_id: str | None):
    """Pick the known listing from a resolved/ambiguous read. Never guesses."""
    if not result.ok or listing_id is None:
        return None
    if result.result is ListingResult.RESOLVED:
        if result.listing is not None and result.listing.listing_id == listing_id:
            return result.listing
        return None
    if result.result is ListingResult.AMBIGUOUS:
        return next(
            (
                candidate
                for candidate in result.candidates
                if candidate.listing_id == listing_id
            ),
            None,
        )
    return None


def build_graph(
    client: ToolClient,
    checkpointer: BaseCheckpointSaver | None = None,
):
    db_path = client.settings.db_path

    def finish(
        state: AgentState,
        status: TaskStatus,
        code: ErrorCode | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        tasks.update_task(
            db_path, state["task_id"], status=status, error_code=code, **updates
        )
        result: dict[str, Any] = {"status": status.value}
        if code is not None:
            result["error_code"] = code.value
        return result

    # -- 1. stock ---------------------------------------------------------

    def check_stock(state: AgentState) -> dict[str, Any]:
        result = client.check_inventory(
            CheckInventoryInput(sku_id=state["sku_id"], spec=state["spec"])
        )
        if not result.ok:
            code = result.error_code or ErrorCode.INTERNAL_ERROR
            if code is ErrorCode.INVENTORY_NOT_FOUND:
                return finish(state, TaskStatus.ABORTED_NOT_FOUND, code)
            # Timeout / circuit / internal: state unknown, stop before writes.
            return finish(state, TaskStatus.ABORTED_UNKNOWN_STATE, code)

        if result.quantity is None or result.quantity <= 0:
            return finish(state, TaskStatus.ABORTED_NO_STOCK, ErrorCode.INSUFFICIENT_STOCK)

        return {
            "inventory_record_id": result.inventory_record_id,
            "status": TaskStatus.RECEIVED.value,
        }

    def route_after_stock(state: AgentState) -> str:
        return END if _is_terminal(state) else "resolve_listing"

    # -- 2. mapping -------------------------------------------------------

    def resolve_listing(state: AgentState) -> dict[str, Any]:
        result = client.get_listing(GetListingInput(sku_id=state["sku_id"]))
        if not result.ok:
            return finish(
                state,
                TaskStatus.ABORTED_UNKNOWN_STATE,
                result.error_code or ErrorCode.INTERNAL_ERROR,
            )
        if result.result is ListingResult.NOT_FOUND:
            return finish(
                state, TaskStatus.ABORTED_NOT_FOUND, ErrorCode.MAPPING_NOT_FOUND
            )

        if result.result is ListingResult.AMBIGUOUS:
            candidates = result.candidates
            tasks.update_task(
                db_path,
                state["task_id"],
                status=TaskStatus.SUSPENDED_AMBIGUOUS,
                candidates=candidates,
            )
            choice = interrupt(
                {
                    "type": "mapping_required",
                    "task_id": state["task_id"],
                    "sku_id": state["sku_id"],
                    "spec": state["spec"],
                    "candidates": [
                        candidate.model_dump(mode="json") for candidate in candidates
                    ],
                }
            )
            chosen = None
            if isinstance(choice, dict):
                chosen = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.listing_id == choice.get("listing_id")
                    ),
                    None,
                )
            if chosen is None:
                return finish(state, TaskStatus.ABORTED_FAILED, ErrorCode.MAPPING_MISMATCH)
            if (
                chosen.spec != state["spec"]
                or chosen.inventory_record_id != state["inventory_record_id"]
            ):
                return finish(state, TaskStatus.ABORTED_FAILED, ErrorCode.MAPPING_MISMATCH)

            tasks.update_task(
                db_path,
                state["task_id"],
                status=TaskStatus.RECEIVED,
                listing_id=chosen.listing_id,
                original_price=chosen.price,
            )
            return {
                "listing_id": chosen.listing_id,
                "original_price": str(chosen.price),
                "status": TaskStatus.RECEIVED.value,
            }

        listing = result.listing
        if listing is None:
            return finish(
                state, TaskStatus.ABORTED_FAILED, ErrorCode.MAPPING_NOT_FOUND
            )
        if (
            listing.spec != state["spec"]
            or listing.inventory_record_id != state["inventory_record_id"]
        ):
            return finish(state, TaskStatus.ABORTED_FAILED, ErrorCode.MAPPING_MISMATCH)

        tasks.update_task(
            db_path,
            state["task_id"],
            listing_id=listing.listing_id,
            original_price=listing.price,
        )
        return {
            "listing_id": listing.listing_id,
            "original_price": str(listing.price),
            "status": TaskStatus.RECEIVED.value,
        }

    def route_after_mapping(state: AgentState) -> str:
        return END if _is_terminal(state) else "stage_price"

    # -- 3. stage price ---------------------------------------------------

    def _adopt_reconciled_pending(state: AgentState) -> dict[str, Any] | None:
        """One read-only reconciliation after a write timeout / conflict."""
        result = client.get_listing(GetListingInput(sku_id=state["sku_id"]))
        listing = _select_listing(result, state.get("listing_id"))
        if listing is None or listing.pending is None:
            return None
        pending = listing.pending
        if pending.staged_price != _target(state):
            return None
        if pending.approval_status not in (
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
        ):
            return None
        if pending.stage not in (PendingStage.PRICE_STAGED, PendingStage.PUBLISH_PENDING):
            return None

        tasks.update_task(
            db_path,
            state["task_id"],
            status=TaskStatus.AWAITING_APPROVAL,
            approval_id=pending.approval_id,
            original_price=pending.original_price,
        )
        return {
            "approval_id": pending.approval_id,
            "original_price": str(pending.original_price),
            "status": TaskStatus.AWAITING_APPROVAL.value,
        }

    def stage_price(state: AgentState) -> dict[str, Any]:
        result = client.update_price(
            UpdatePriceInput(
                listing_id=state["listing_id"],
                new_price=_target(state),
                proposal_reason=state["reason"],
            )
        )
        if result.ok:
            tasks.update_task(
                db_path,
                state["task_id"],
                status=TaskStatus.AWAITING_APPROVAL,
                approval_id=result.approval_id,
                original_price=result.original_price,
            )
            return {
                "approval_id": result.approval_id,
                "original_price": str(result.original_price),
                "status": TaskStatus.AWAITING_APPROVAL.value,
            }

        if result.error_code is ErrorCode.PENDING_APPROVAL_EXISTS:
            # Someone else's live proposal owns this listing. Never adopt it:
            # two tasks must not share one approval.
            return finish(
                state, TaskStatus.ABORTED_FAILED, result.error_code
            )

        if result.error_code in (
            ErrorCode.TOOL_TIMEOUT,
            ErrorCode.TOOL_UNAVAILABLE,
            ErrorCode.CIRCUIT_OPEN,
        ):
            adopted = _adopt_reconciled_pending(state)
            if adopted is not None:
                return adopted
            return finish(
                state, TaskStatus.ABORTED_UNKNOWN_STATE, result.error_code
            )

        return finish(
            state,
            TaskStatus.ABORTED_FAILED,
            result.error_code or ErrorCode.INTERNAL_ERROR,
        )

    def route_after_stage(state: AgentState) -> str:
        return END if _is_terminal(state) else "await_approval"

    # -- 4. human approval gate ------------------------------------------

    def await_approval(state: AgentState) -> dict[str, Any]:
        tasks.update_task(
            db_path, state["task_id"], status=TaskStatus.AWAITING_APPROVAL
        )
        interrupt(
            {
                "type": "approval_required",
                "task_id": state["task_id"],
                "approval_id": state["approval_id"],
                "listing_id": state["listing_id"],
                "target_price": state["target_price"],
            }
        )

        # The approval queue is the source of truth; the resume payload only
        # wakes the graph.
        approval = client.get_approval(state["approval_id"])
        if approval is None:
            return finish(
                state, TaskStatus.ABORTED_FAILED, ErrorCode.APPROVAL_NOT_FOUND
            )
        if approval.status is ApprovalStatus.REJECTED:
            return finish(
                state, TaskStatus.ROLLED_BACK, ErrorCode.APPROVAL_REJECTED
            )
        if approval.status is ApprovalStatus.ABANDONED:
            return finish(
                state, TaskStatus.ROLLED_BACK, ErrorCode.INSUFFICIENT_STOCK
            )
        if approval.status is ApprovalStatus.CONSUMED:
            tasks.update_task(
                db_path,
                state["task_id"],
                status=TaskStatus.PUBLISHED,
                final_price=approval.new_price,
            )
            return {
                "status": TaskStatus.PUBLISHED.value,
                "approval_outcome": "committed",
            }
        if approval.status is ApprovalStatus.APPROVED:
            return {
                "status": TaskStatus.AWAITING_APPROVAL.value,
                "approval_outcome": "approved",
            }
        # Still pending: the API should not have resumed, but never publish.
        return {"approval_outcome": "pending"}

    def route_after_approval(state: AgentState) -> str:
        if _is_terminal(state):
            return END
        outcome = state.get("approval_outcome")
        if outcome == "approved":
            return "recheck_stock"
        if outcome == "committed":
            return "verify"
        return END

    # -- 5. publish-time inventory gate ----------------------------------

    def _abandon_proposal(state: AgentState, reason: str) -> dict[str, Any] | None:
        """Withdraw the approved proposal; never publish when this fails."""
        try:
            client.abandon_approval(state["approval_id"], reason)
        except ToolFailure as exc:
            return finish(state, TaskStatus.ABORTED_FAILED, exc.code)
        except Exception:  # noqa: BLE001 - unknown cleanup outcome, stop
            return finish(
                state, TaskStatus.ABORTED_UNKNOWN_STATE, ErrorCode.STATE_UNKNOWN
            )
        return None

    def recheck_stock(state: AgentState) -> dict[str, Any]:
        """A point-in-time stock check immediately before the commit."""
        result = client.check_inventory(
            CheckInventoryInput(sku_id=state["sku_id"], spec=state["spec"])
        )
        if not result.ok:
            code = result.error_code or ErrorCode.INTERNAL_ERROR
            # We cannot verify stock, so we will not publish. Withdraw the
            # proposal locally (no mall write) instead of leaving an approved
            # staging row behind for a task that has already stopped.
            failure = _abandon_proposal(
                state, f"发布前库存复核失败：{code.value}，系统撤回提案"
            )
            if failure is not None:
                return failure
            if code is ErrorCode.INVENTORY_NOT_FOUND:
                return finish(state, TaskStatus.ABORTED_NOT_FOUND, code)
            return finish(state, TaskStatus.ABORTED_UNKNOWN_STATE, code)

        if result.quantity is None or result.quantity <= 0:
            failure = _abandon_proposal(
                state,
                f"发布前库存复核不通过：quantity={result.quantity}",
            )
            if failure is not None:
                return failure
            return finish(
                state, TaskStatus.ROLLED_BACK, ErrorCode.INSUFFICIENT_STOCK
            )

        return {}

    def route_after_recheck(state: AgentState) -> str:
        return END if _is_terminal(state) else "publish"

    # -- 6. atomic publish ------------------------------------------------

    def publish(state: AgentState) -> dict[str, Any]:
        result = client.publish_listing(
            PublishListingInput(
                listing_id=state["listing_id"],
                approval_id=state["approval_id"],
            )
        )
        if result.ok:
            tasks.update_task(
                db_path,
                state["task_id"],
                status=TaskStatus.PUBLISHED,
                final_price=result.final_price,
            )
            return {"status": TaskStatus.PUBLISHED.value}

        if result.error_code in (
            ErrorCode.INSUFFICIENT_STOCK,
            ErrorCode.INVENTORY_NOT_FOUND,
        ):
            # The in-transaction stock gate refused the commit. Nothing was
            # written to the mall; withdraw the approved staging row.
            failure = _abandon_proposal(
                state,
                f"提交事务内库存校验失败：{result.error_code.value}，系统撤回提案",
            )
            if failure is not None:
                return failure
            if result.error_code is ErrorCode.INSUFFICIENT_STOCK:
                return finish(
                    state, TaskStatus.ROLLED_BACK, ErrorCode.INSUFFICIENT_STOCK
                )
            return finish(
                state, TaskStatus.ABORTED_NOT_FOUND, ErrorCode.INVENTORY_NOT_FOUND
            )

        if result.error_code in (
            ErrorCode.TOOL_TIMEOUT,
            ErrorCode.TOOL_UNAVAILABLE,
            ErrorCode.CIRCUIT_OPEN,
            ErrorCode.APPROVAL_ALREADY_CONSUMED,
        ):
            reconciled = client.get_listing(GetListingInput(sku_id=state["sku_id"]))
            listing = _select_listing(reconciled, state.get("listing_id"))
            if (
                listing is not None
                and listing.price == _target(state)
                and listing.status is ListingStatus.PUBLISHED
            ):
                tasks.update_task(
                    db_path,
                    state["task_id"],
                    status=TaskStatus.PUBLISHED,
                    final_price=listing.price,
                )
                return {"status": TaskStatus.PUBLISHED.value}

            if result.error_code is ErrorCode.APPROVAL_ALREADY_CONSUMED:
                return finish(
                    state, TaskStatus.ABORTED_FAILED, result.error_code
                )
            # Write outcome unknown: stop, never retry the write.
            return finish(
                state, TaskStatus.ABORTED_UNKNOWN_STATE, result.error_code
            )

        if result.error_code is ErrorCode.APPROVAL_REJECTED:
            return finish(state, TaskStatus.ROLLED_BACK, result.error_code)

        return finish(
            state,
            TaskStatus.ABORTED_FAILED,
            result.error_code or ErrorCode.INTERNAL_ERROR,
        )

    def route_after_publish(state: AgentState) -> str:
        if state.get("status") == TaskStatus.PUBLISHED.value:
            return "verify"
        return END

    # -- 7. final read-only verification ---------------------------------

    def verify(state: AgentState) -> dict[str, Any]:
        result = client.get_listing(GetListingInput(sku_id=state["sku_id"]))
        if not result.ok:
            return finish(
                state,
                TaskStatus.FAILED_VERIFICATION,
                result.error_code or ErrorCode.INTERNAL_ERROR,
            )

        listing = _select_listing(result, state.get("listing_id"))
        if listing is None:
            verification = _build_verification(
                state.get("listing_id") or "", _target(state), None
            )
            return finish(
                state,
                TaskStatus.FAILED_VERIFICATION,
                ErrorCode.MAPPING_NOT_FOUND,
                verification=TaskVerification(
                    result=verification, verified_at=utcnow()
                ),
            )

        verification = _build_verification(
            listing.listing_id, _target(state), listing
        )
        matched = verification.matched
        task_verification = TaskVerification(
            result=verification, verified_at=utcnow()
        )
        status = (
            TaskStatus.VERIFIED if matched else TaskStatus.FAILED_VERIFICATION
        )
        tasks.update_task(
            db_path,
            state["task_id"],
            status=status,
            final_price=listing.price,
            verification=task_verification,
        )
        return {
            "status": status.value,
            "verification": task_verification.model_dump(mode="json"),
        }

    builder = StateGraph(AgentState)
    builder.add_node("check_stock", check_stock)
    builder.add_node("resolve_listing", resolve_listing)
    builder.add_node("stage_price", stage_price)
    builder.add_node("await_approval", await_approval)
    builder.add_node("recheck_stock", recheck_stock)
    builder.add_node("publish", publish)
    builder.add_node("verify", verify)

    builder.add_edge(START, "check_stock")
    builder.add_conditional_edges(
        "check_stock", route_after_stock, {"resolve_listing": "resolve_listing", END: END}
    )
    builder.add_conditional_edges(
        "resolve_listing", route_after_mapping, {"stage_price": "stage_price", END: END}
    )
    builder.add_conditional_edges(
        "stage_price",
        route_after_stage,
        {"await_approval": "await_approval", END: END},
    )
    builder.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {"recheck_stock": "recheck_stock", "verify": "verify", END: END},
    )
    builder.add_conditional_edges(
        "recheck_stock",
        route_after_recheck,
        {"publish": "publish", END: END},
    )
    builder.add_conditional_edges(
        "publish", route_after_publish, {"verify": "verify", END: END}
    )
    builder.add_edge("verify", END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Service helpers used by FastAPI and tests
# ---------------------------------------------------------------------------


def initial_state(task: TaskView) -> AgentState:
    return {
        "task_id": task.task_id,
        "sku_id": task.sku_id,
        "spec": task.spec,
        "target_price": str(task.target_price),
        "reason": task.reason,
        "status": TaskStatus.RECEIVED.value,
    }


def thread_config(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": task_id}}


def start_task(
    client: ToolClient,
    graph,
    request: CreateTaskRequest,
    *,
    idempotency_key: str | None = None,
    submitted_by: str | None = None,
) -> TaskView:
    task = tasks.create_task(
        client.settings.db_path,
        request,
        idempotency_key=idempotency_key,
        submitted_by=submitted_by,
    )
    with audit.task_context(task.task_id):
        graph.invoke(initial_state(task), thread_config(task.task_id))
    refreshed = tasks.get_task(client.settings.db_path, task.task_id)
    assert refreshed is not None
    return refreshed


def resume_task(client: ToolClient, graph, task_id: str, value: Any) -> TaskView:
    with audit.task_context(task_id):
        graph.invoke(Command(resume=value), thread_config(task_id))
    refreshed = tasks.get_task(client.settings.db_path, task_id)
    assert refreshed is not None
    return refreshed


def verify_published_task(client: ToolClient, task_id: str) -> TaskView:
    """Finish a recovered task whose mall commit already happened.

    This is intentionally read-only on the mall side: it re-reads the listing,
    records the same verification structure as the graph's ``verify`` node, and
    never calls ``publish_listing`` again.
    """
    db_path = client.settings.db_path
    task = tasks.get_task(db_path, task_id)
    if task is None:
        raise ToolFailure(ErrorCode.MAPPING_NOT_FOUND, f"task {task_id!r} not found")
    if task.status is not TaskStatus.PUBLISHED:
        raise ToolFailure(
            ErrorCode.INVALID_STATE,
            f"task is {task.status.value}, not awaiting verification",
        )

    with audit.task_context(task_id):
        result = client.get_listing(GetListingInput(sku_id=task.sku_id))
    if not result.ok:
        return tasks.update_task(
            db_path,
            task_id,
            status=TaskStatus.FAILED_VERIFICATION,
            error_code=result.error_code or ErrorCode.INTERNAL_ERROR,
        )

    listing = _select_listing(result, task.listing_id)
    verification = _build_verification(
        task.listing_id or "", task.target_price, listing
    )
    task_verification = TaskVerification(
        result=verification, verified_at=utcnow()
    )
    status = (
        TaskStatus.VERIFIED if verification.matched else TaskStatus.FAILED_VERIFICATION
    )
    return tasks.update_task(
        db_path,
        task_id,
        status=status,
        final_price=listing.price if listing is not None else None,
        verification=task_verification,
        error_code=(
            None
            if listing is not None
            else ErrorCode.MAPPING_NOT_FOUND
        ),
    )


def _approval_matches_decision(approval, decision: ApprovalDecision) -> bool:
    if decision is ApprovalDecision.APPROVE:
        return approval.status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.CONSUMED,
        )
    return approval.status in (
        ApprovalStatus.REJECTED,
        ApprovalStatus.ABANDONED,
    )


def decide_and_resume(
    client: ToolClient,
    graph,
    approval_id: str,
    decision: ApprovalDecision,
    note: str | None = None,
    decided_by: str | None = None,
) -> TaskView:
    approval = client.get_approval(approval_id)
    if approval is None:
        raise ToolFailure(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"approval {approval_id!r} does not exist",
        )
    task = tasks.find_task_by_approval(client.settings.db_path, approval_id)
    if task is None:
        raise ToolFailure(
            ErrorCode.APPROVAL_NOT_FOUND,
            f"no task is waiting on approval {approval_id!r}",
        )

    if task.status is not TaskStatus.AWAITING_APPROVAL:
        # Idempotent replay: replaying the decision that already finished
        # returns the current task instead of an error.
        if _approval_matches_decision(approval, decision):
            return task
        raise ToolFailure(
            ErrorCode.INVALID_STATE,
            f"task is {task.status.value}, not awaiting approval",
        )

    if approval.status is ApprovalStatus.PENDING:
        client.decide_approval(approval_id, decision, note, decided_by)
    elif not _approval_matches_decision(approval, decision):
        raise ToolFailure(
            ErrorCode.APPROVAL_ALREADY_DECIDED,
            f"approval {approval_id!r} is already {approval.status.value}",
        )
    # Otherwise the decision is already recorded (for example the process
    # died between recording it and resuming the graph): just resume.

    return resume_task(
        client,
        graph,
        task.task_id,
        {"decision": decision.value, "note": note},
    )


def confirm_mapping_and_resume(
    client: ToolClient, graph, task_id: str, listing_id: str
) -> TaskView:
    task = tasks.get_task(client.settings.db_path, task_id)
    if task is None:
        raise ToolFailure(ErrorCode.MAPPING_NOT_FOUND, f"task {task_id!r} not found")
    if task.status is not TaskStatus.SUSPENDED_AMBIGUOUS:
        raise ToolFailure(
            ErrorCode.INVALID_STATE,
            f"task is {task.status.value}, not suspended for mapping",
        )
    if listing_id not in {candidate.listing_id for candidate in task.candidates}:
        raise ToolFailure(
            ErrorCode.MAPPING_MISMATCH,
            f"listing {listing_id!r} is not one of the task's candidates",
        )
    return resume_task(client, graph, task_id, {"listing_id": listing_id})
