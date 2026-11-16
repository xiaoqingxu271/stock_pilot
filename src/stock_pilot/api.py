"""FastAPI surface.

Three human-facing operations:
  POST /tasks                          submit a repricing task
  POST /approvals/{approval_id}/decision   approve or reject
  POST /tasks/{task_id}/mapping            resolve an ambiguous listing

Read-only endpoints expose task and approval state for the demo UI.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot import (
    approval,
    audit,
    auth,
    mall,
    notifications,
    recovery,
    reservations,
    tasks,
)
from stock_pilot.agent import (
    build_graph,
    confirm_mapping_and_resume,
    decide_and_resume,
    resume_task,
    start_task,
    verify_published_task,
)
from stock_pilot.auth import Principal, Role
from stock_pilot.clients import ToolClient, create_tool_client
from stock_pilot.config import Settings
from stock_pilot.db import utcnow
from stock_pilot.enums import (
    ApprovalStatus,
    ErrorCode,
    ListingResult,
    ListingStatus,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.metrics import render_metrics
from stock_pilot.models import (
    AbandonApprovalRequest,
    ApprovalDecisionRequest,
    ApprovalQueueItem,
    ApprovalView,
    CreateTaskRequest,
    GetListingInput,
    HealthStatus,
    ListReservationsOutput,
    MappingConfirmRequest,
    RecoveryIssue,
    ReservationGcResponse,
    TaskView,
    ToolEvent,
)
from stock_pilot.tools import StockPilotTools

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_NOT_FOUND = {ErrorCode.APPROVAL_NOT_FOUND, ErrorCode.MAPPING_NOT_FOUND}
_CONFLICT = {
    ErrorCode.APPROVAL_ALREADY_DECIDED,
    ErrorCode.INVALID_STATE,
    ErrorCode.PENDING_APPROVAL_EXISTS,
    ErrorCode.IDEMPOTENCY_CONFLICT,
}
_UPSTREAM = {
    ErrorCode.TOOL_TIMEOUT,
    ErrorCode.TOOL_UNAVAILABLE,
    ErrorCode.CIRCUIT_OPEN,
}


def _http_error(exc: ToolFailure) -> HTTPException:
    if exc.code in _NOT_FOUND:
        status = 404
    elif exc.code in _CONFLICT:
        status = 409
    else:
        status = 400
    return HTTPException(status_code=status, detail=exc.message)


def create_app(
    settings: Settings | None = None,
    tools: StockPilotTools | None = None,
    checkpointer=None,
    tool_client: ToolClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    client: ToolClient = tool_client or tools or create_tool_client(settings)
    if checkpointer is None:
        conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    graph = build_graph(client, checkpointer)
    db_path = settings.db_path
    approval_db = settings.approval_db
    mall_db = settings.store_mall
    # Notification-only worker: it is constructed without any tool client.
    outbox_worker = notifications.OutboxWorker(
        db_path, interval_seconds=settings.outbox_interval_seconds
    )

    def scan_recovery():
        return recovery.scan(
            db_path,
            mall_db_path=mall_db,
            inventory_db_path=settings.store_inventory,
            reservation_ttl_seconds=settings.reservation_ttl_seconds,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        issues = scan_recovery()
        if issues:
            logger.warning(
                "recovery scan found %d task(s) needing attention: %s",
                len(issues),
                ", ".join(f"{issue.task_id}:{issue.kind.value}" for issue in issues),
            )
        outbox_worker.start()
        try:
            yield
        finally:
            outbox_worker.stop()
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()

    app = FastAPI(
        title="StockPilot",
        version="0.1.0",
        description="Inventory-driven repricing and publishing with approval gates.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.tools = client
    app.state.tool_client = client
    app.state.graph = graph
    app.state.checkpointer = checkpointer
    app.state.outbox_worker = outbox_worker

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _require(role: Role | None):
        """Enforce a role, or merely authentication when ``role`` is None.

        No configured tokens means local demo mode (returns ``None``). As soon
        as any token exists, every mutation requires an appropriate token;
        read endpoints additionally require one only when
        ``STOCK_PILOT_REQUIRE_READ_AUTH`` is on.
        """

        def dependency(
            authorization: str | None = Header(
                default=None, alias="Authorization"
            ),
        ) -> Principal | None:
            principal = auth.principal_from_authorization(settings, authorization)
            if principal is not None:
                if role is not None and not auth.permits(principal, role):
                    raise HTTPException(
                        status_code=403,
                        detail=f"role {role.value!r} required",
                    )
                return principal
            if not auth.auth_enabled(settings):
                return None
            if role is None and not settings.require_read_auth:
                return None
            raise HTTPException(
                status_code=401,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return dependency

    @app.get("/", include_in_schema=False)
    def approval_ui() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    def _replay_existing(existing: TaskView, request: CreateTaskRequest) -> TaskView:
        if (
            existing.sku_id != request.sku_id
            or existing.spec != request.spec
            or existing.target_price != request.target_price
            or existing.reason != request.reason
        ):
            raise _http_error(
                ToolFailure(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "Idempotency-Key was already used with a different request",
                )
            )
        return existing

    @app.post("/tasks", response_model=TaskView, status_code=201)
    def submit_task(
        request: CreateTaskRequest,
        response: Response,
        principal: Principal | None = Depends(_require(Role.AGENT)),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", max_length=128
        ),
    ) -> TaskView:
        key = idempotency_key.strip() if idempotency_key is not None else None
        if idempotency_key is not None and not key:
            raise HTTPException(
                status_code=400, detail="Idempotency-Key must not be blank"
            )
        if key is not None:
            existing = tasks.find_task_by_idempotency_key(db_path, key)
            if existing is not None:
                response.status_code = 200
                return _replay_existing(existing, request)
        try:
            return start_task(
                client,
                graph,
                request,
                idempotency_key=key,
                submitted_by=principal.name if principal is not None else None,
            )
        except sqlite3.IntegrityError:
            existing = (
                tasks.find_task_by_idempotency_key(db_path, key)
                if key is not None
                else None
            )
            if existing is None:
                raise
            response.status_code = 200
            return _replay_existing(existing, request)

    @app.get("/tasks", response_model=list[TaskView])
    def list_task_records(
        _principal: Principal | None = Depends(_require(None)),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        return tasks.list_tasks(db_path, limit=limit)

    @app.get("/tasks/{task_id}", response_model=TaskView)
    def get_task_record(
        task_id: str,
        _principal: Principal | None = Depends(_require(None)),
    ) -> TaskView:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.get("/tasks/{task_id}/events", response_model=list[ToolEvent])
    def get_task_events(
        task_id: str,
        _principal: Principal | None = Depends(_require(None)),
    ) -> list[ToolEvent]:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return audit.list_tool_events(db_path, task_id)

    @app.get("/recovery", response_model=list[RecoveryIssue])
    def list_recovery_issues(
        _principal: Principal | None = Depends(_require(None)),
    ) -> list[RecoveryIssue]:
        return scan_recovery()

    @app.get("/reservations", response_model=ListReservationsOutput)
    def list_stock_reservations(
        _principal: Principal | None = Depends(_require(None)),
    ) -> ListReservationsOutput:
        if not settings.split_stores:
            # Reservations are a split-store saga step; shared mode has none.
            return ListReservationsOutput(checked_at=utcnow())
        result = client.list_reservations()
        if not result.ok:
            raise HTTPException(
                status_code=503,
                detail=result.message or "cannot read stock reservations",
            )
        return result

    @app.post("/reservations/gc", response_model=ReservationGcResponse)
    def sweep_stock_reservations(
        _principal: Principal | None = Depends(_require(Role.OPERATOR)),
    ) -> ReservationGcResponse:
        """Release only reservations whose approval can no longer publish."""
        if not settings.split_stores:
            return ReservationGcResponse(
                checked_at=utcnow(), released=0, retained=0, stale=0, failed=0
            )
        try:
            return reservations.sweep(
                client, ttl_seconds=settings.reservation_ttl_seconds
            )
        except ToolFailure as exc:
            if exc.code in _UPSTREAM:
                raise HTTPException(
                    status_code=503,
                    detail=f"reservation sweep unavailable: {exc.message}",
                ) from exc
            raise _http_error(exc) from exc

    @app.get("/healthz", response_model=HealthStatus)
    def health(response: Response) -> HealthStatus:
        db_ok = True
        stores = dict.fromkeys(
            (db_path, mall_db, settings.store_inventory)
        )
        try:
            for path in stores:
                conn = sqlite3.connect(path, timeout=1.0)
                try:
                    conn.execute("SELECT 1").fetchall()
                finally:
                    conn.close()
        except sqlite3.Error:
            db_ok = False

        probe = getattr(client, "is_available", None)
        try:
            tool_ok = bool(probe()) if callable(probe) else True
        except Exception:  # noqa: BLE001 - a failing probe means unavailable
            tool_ok = False

        pending = 0
        issues = 0
        if db_ok:
            try:
                pending = len(
                    approval.list_approvals(approval_db, ApprovalStatus.PENDING)
                )
                issues = len(scan_recovery())
            except sqlite3.Error:
                db_ok = False

        healthy = db_ok and tool_ok
        if not healthy:
            response.status_code = 503
        return HealthStatus(
            status="ok" if healthy else "degraded",
            database=db_ok,
            tool_client=tool_ok,
            pending_approvals=pending,
            recovery_issues=issues,
            checked_at=utcnow(),
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics_snapshot() -> Response:
        states = getattr(client, "circuit_states", None)
        circuit_states = states() if callable(states) else {}
        probe = getattr(client, "is_available", None)
        try:
            available = bool(probe()) if callable(probe) else True
        except Exception:  # noqa: BLE001 - a failing probe means unavailable
            available = False
        return Response(
            content=render_metrics(
                db_path,
                mall_db_path=mall_db,
                inventory_db_path=settings.store_inventory,
                reservation_ttl_seconds=settings.reservation_ttl_seconds,
                circuit_states=circuit_states,
                tool_client_available=available,
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/tasks/{task_id}/recover", response_model=TaskView)
    def recover_task(
        task_id: str,
        _principal: Principal | None = Depends(_require(Role.OPERATOR)),
    ) -> TaskView:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status is TaskStatus.PUBLISHED:
            return verify_published_task(client, task_id)
        if task.status is not TaskStatus.AWAITING_APPROVAL:
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    f"task is {task.status.value}, nothing to recover",
                )
            )
        current = approval.get_approval(approval_db, task.approval_id or "")
        if current is None:
            raise _http_error(
                ToolFailure(
                    ErrorCode.APPROVAL_NOT_FOUND,
                    f"approval {task.approval_id!r} does not exist",
                )
            )
        if current.status is ApprovalStatus.PENDING:
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    "approval is still pending; use the decision endpoint",
                )
            )
        if current.status is ApprovalStatus.APPROVED:
            staged = approval.get_staging_for_approval(
                approval_db, current.approval_id
            )
            if staged is None or staged.status is not StagingStatus.PRICE_STAGED:
                raise _http_error(
                    ToolFailure(
                        ErrorCode.STAGING_NOT_FOUND,
                        "approved without a live staged proposal; manual review required",
                    )
                )
        return resume_task(client, graph, task_id, {"recovered": True})

    @app.post("/tasks/{task_id}/mapping", response_model=TaskView)
    def confirm_mapping(
        task_id: str,
        request: MappingConfirmRequest,
        _principal: Principal | None = Depends(_require(Role.OPERATOR)),
    ) -> TaskView:
        try:
            return confirm_mapping_and_resume(
                client, graph, task_id, request.listing_id
            )
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.post("/approvals/{approval_id}/decision", response_model=TaskView)
    def decide(
        approval_id: str,
        request: ApprovalDecisionRequest,
        principal: Principal | None = Depends(_require(Role.OPERATOR)),
    ) -> TaskView:
        # With authentication on, the token identity wins over the body.
        decided_by = principal.name if principal is not None else request.decided_by
        try:
            return decide_and_resume(
                client,
                graph,
                approval_id,
                request.decision,
                request.note,
                decided_by,
            )
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.post("/approvals/{approval_id}/abandon", response_model=ApprovalView)
    def abandon_approval_record(
        approval_id: str,
        request: AbandonApprovalRequest,
        principal: Principal | None = Depends(_require(Role.OPERATOR)),
    ) -> ApprovalView:
        abandoned_by = principal.name if principal is not None else None
        current = approval.get_approval(approval_db, approval_id)
        if current is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if current.status is ApprovalStatus.ABANDONED:
            try:
                return client.abandon_approval(
                    approval_id, request.reason, abandoned_by
                )
            except ToolFailure as exc:
                raise _http_error(exc) from exc
        if current.status is not ApprovalStatus.APPROVED:
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    f"approval is {current.status.value}; only approved proposals"
                    " can be withdrawn",
                )
            )

        listing = mall.get_listing_by_id(mall_db, current.listing_id)
        if listing is None:
            raise _http_error(
                ToolFailure(
                    ErrorCode.LISTING_NOT_FOUND,
                    f"listing {current.listing_id!r} does not exist",
                )
            )

        # Reconciliation is mandatory: never discard staging when the mall
        # may already have committed the proposal.
        observed = client.get_listing(GetListingInput(sku_id=listing.sku_id))
        if not observed.ok:
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    "cannot reconcile with the mall; refusing to withdraw",
                )
            )
        if observed.result is ListingResult.RESOLVED:
            candidate = observed.listing
        elif observed.result is ListingResult.AMBIGUOUS:
            candidate = next(
                (
                    item
                    for item in observed.candidates
                    if item.listing_id == current.listing_id
                ),
                None,
            )
        else:
            candidate = None
        if candidate is None:
            raise _http_error(
                ToolFailure(
                    ErrorCode.LISTING_NOT_FOUND,
                    f"listing {current.listing_id!r} was not found during reconciliation",
                )
            )
        if (
            candidate.price == current.new_price
            and candidate.status is ListingStatus.PUBLISHED
        ):
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    "the mall already committed this proposal; refusing to withdraw",
                )
            )

        try:
            return client.abandon_approval(approval_id, request.reason, abandoned_by)
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.get("/approvals", response_model=list[ApprovalView])
    def list_approval_queue(
        status: ApprovalStatus | None = Query(default=None),
        _principal: Principal | None = Depends(_require(None)),
    ) -> list[ApprovalView]:
        return approval.list_approvals(approval_db, status)

    @app.get("/approvals/queue", response_model=list[ApprovalQueueItem])
    def approval_queue(
        status: ApprovalStatus = Query(default=ApprovalStatus.PENDING),
        limit: int = Query(default=100, ge=1, le=500),
        _principal: Principal | None = Depends(_require(None)),
    ) -> list[ApprovalQueueItem]:
        return approval.list_queue(approval_db, status=status, limit=limit)

    @app.get("/approvals/{approval_id}", response_model=ApprovalView)
    def get_approval_record(
        approval_id: str,
        _principal: Principal | None = Depends(_require(None)),
    ) -> ApprovalView:
        record = approval.get_approval(approval_db, approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return record

    return app


def main() -> None:
    import uvicorn

    from stock_pilot.logging_config import configure_logging

    configure_logging()
    uvicorn.run(
        "stock_pilot.api:create_app",
        factory=True,
        host=os.environ.get("STOCK_PILOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("STOCK_PILOT_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
