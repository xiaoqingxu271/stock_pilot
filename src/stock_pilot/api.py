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

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot import approval, audit, recovery, tasks
from stock_pilot.agent import (
    build_graph,
    confirm_mapping_and_resume,
    decide_and_resume,
    resume_task,
    start_task,
)
from stock_pilot.clients import ToolClient, create_tool_client
from stock_pilot.config import Settings
from stock_pilot.enums import (
    ApprovalStatus,
    ErrorCode,
    StagingStatus,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    ApprovalDecisionRequest,
    ApprovalQueueItem,
    ApprovalView,
    CreateTaskRequest,
    MappingConfirmRequest,
    RecoveryIssue,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        issues = recovery.scan(db_path)
        if issues:
            logger.warning(
                "recovery scan found %d task(s) needing attention: %s",
                len(issues),
                ", ".join(f"{issue.task_id}:{issue.kind.value}" for issue in issues),
            )
        yield
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

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
                client, graph, request, idempotency_key=key
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
    def list_task_records(limit: int = Query(default=100, ge=1, le=500)):
        return tasks.list_tasks(db_path, limit=limit)

    @app.get("/tasks/{task_id}", response_model=TaskView)
    def get_task_record(task_id: str) -> TaskView:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.get("/tasks/{task_id}/events", response_model=list[ToolEvent])
    def get_task_events(task_id: str) -> list[ToolEvent]:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return audit.list_tool_events(db_path, task_id)

    @app.get("/recovery", response_model=list[RecoveryIssue])
    def list_recovery_issues() -> list[RecoveryIssue]:
        return recovery.scan(db_path)

    @app.post("/tasks/{task_id}/recover", response_model=TaskView)
    def recover_task(task_id: str) -> TaskView:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status is not TaskStatus.AWAITING_APPROVAL:
            raise _http_error(
                ToolFailure(
                    ErrorCode.INVALID_STATE,
                    f"task is {task.status.value}, nothing to recover",
                )
            )
        current = approval.get_approval(db_path, task.approval_id or "")
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
            staged = approval.get_staging_for_approval(db_path, current.approval_id)
            if staged is None or staged.status is not StagingStatus.PRICE_STAGED:
                raise _http_error(
                    ToolFailure(
                        ErrorCode.STAGING_NOT_FOUND,
                        "approved without a live staged proposal; manual review required",
                    )
                )
        return resume_task(client, graph, task_id, {"recovered": True})

    @app.post("/tasks/{task_id}/mapping", response_model=TaskView)
    def confirm_mapping(task_id: str, request: MappingConfirmRequest) -> TaskView:
        try:
            return confirm_mapping_and_resume(
                client, graph, task_id, request.listing_id
            )
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.post("/approvals/{approval_id}/decision", response_model=TaskView)
    def decide(approval_id: str, request: ApprovalDecisionRequest) -> TaskView:
        try:
            return decide_and_resume(
                client,
                graph,
                approval_id,
                request.decision,
                request.note,
                request.decided_by,
            )
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.get("/approvals", response_model=list[ApprovalView])
    def list_approval_queue(
        status: ApprovalStatus | None = Query(default=None),
    ) -> list[ApprovalView]:
        return approval.list_approvals(db_path, status)

    @app.get("/approvals/queue", response_model=list[ApprovalQueueItem])
    def approval_queue(
        status: ApprovalStatus = Query(default=ApprovalStatus.PENDING),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[ApprovalQueueItem]:
        return approval.list_queue(db_path, status=status, limit=limit)

    @app.get("/approvals/{approval_id}", response_model=ApprovalView)
    def get_approval_record(approval_id: str) -> ApprovalView:
        record = approval.get_approval(db_path, approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return record

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "stock_pilot.api:create_app",
        factory=True,
        host=os.environ.get("STOCK_PILOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("STOCK_PILOT_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
