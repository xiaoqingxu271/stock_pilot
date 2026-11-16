"""FastAPI surface.

Three human-facing operations:
  POST /tasks                          submit a repricing task
  POST /approvals/{approval_id}/decision   approve or reject
  POST /tasks/{task_id}/mapping            resolve an ambiguous listing

Read-only endpoints expose task and approval state for the demo UI.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from stock_pilot import approval, tasks
from stock_pilot.agent import (
    build_graph,
    confirm_mapping_and_resume,
    decide_and_resume,
    start_task,
)
from stock_pilot.config import Settings
from stock_pilot.enums import ApprovalStatus, ErrorCode
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    ApprovalDecisionRequest,
    ApprovalQueueItem,
    ApprovalView,
    CreateTaskRequest,
    MappingConfirmRequest,
    TaskView,
)
from stock_pilot.tools import StockPilotTools

STATIC_DIR = Path(__file__).resolve().parent / "static"

_NOT_FOUND = {ErrorCode.APPROVAL_NOT_FOUND, ErrorCode.MAPPING_NOT_FOUND}
_CONFLICT = {
    ErrorCode.APPROVAL_ALREADY_DECIDED,
    ErrorCode.INVALID_STATE,
    ErrorCode.PENDING_APPROVAL_EXISTS,
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
) -> FastAPI:
    settings = settings or Settings.from_env()
    tools = tools or StockPilotTools(settings)
    if checkpointer is None:
        conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    graph = build_graph(tools, checkpointer)
    db_path = settings.db_path

    app = FastAPI(
        title="StockPilot",
        version="0.1.0",
        description="Inventory-driven repricing and publishing with approval gates.",
    )
    app.state.settings = settings
    app.state.tools = tools
    app.state.graph = graph
    app.state.checkpointer = checkpointer

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def approval_ui() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/tasks", response_model=TaskView, status_code=201)
    def submit_task(request: CreateTaskRequest) -> TaskView:
        return start_task(tools, graph, request)

    @app.get("/tasks", response_model=list[TaskView])
    def list_task_records(limit: int = Query(default=100, ge=1, le=500)):
        return tasks.list_tasks(db_path, limit=limit)

    @app.get("/tasks/{task_id}", response_model=TaskView)
    def get_task_record(task_id: str) -> TaskView:
        task = tasks.get_task(db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.post("/tasks/{task_id}/mapping", response_model=TaskView)
    def confirm_mapping(task_id: str, request: MappingConfirmRequest) -> TaskView:
        try:
            return confirm_mapping_and_resume(
                tools, graph, task_id, request.listing_id
            )
        except ToolFailure as exc:
            raise _http_error(exc) from exc

    @app.post("/approvals/{approval_id}/decision", response_model=TaskView)
    def decide(approval_id: str, request: ApprovalDecisionRequest) -> TaskView:
        try:
            return decide_and_resume(
                tools, graph, approval_id, request.decision, request.note
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
