"""Tool-client boundary used by the LangGraph orchestrator.

``StockPilotTools`` is the in-process implementation. ``MCPToolClient`` runs
the same four tools over the real MCP protocol (stdio), while approval-queue
reads/decisions stay local because the queue is a human admin plane, not one
of the four contracted MCP tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stock_pilot import approval as approval_queue
from stock_pilot.audit import make_event_sink
from stock_pilot.config import PROJECT_ROOT, Settings
from stock_pilot.db import initialize_database
from stock_pilot.enums import ApprovalDecision, ErrorCode
from stock_pilot.errors import (
    CircuitOpenError,
    ToolExecutionError,
    ToolFailure,
    ToolTimeoutError,
    ToolUnavailableError,
)
from stock_pilot.models import (
    ApprovalView,
    CheckInventoryInput,
    CheckInventoryOutput,
    GetListingInput,
    GetListingOutput,
    PublishListingInput,
    PublishListingOutput,
    ToolOutput,
    UpdatePriceInput,
    UpdatePriceOutput,
)
from stock_pilot.runner import ToolExecutor
from stock_pilot.tools import StockPilotTools

TOutput = TypeVar("TOutput", bound=ToolOutput)


@runtime_checkable
class ToolClient(Protocol):
    settings: Settings

    def check_inventory(self, request: CheckInventoryInput) -> CheckInventoryOutput: ...

    def get_listing(self, request: GetListingInput) -> GetListingOutput: ...

    def update_price(self, request: UpdatePriceInput) -> UpdatePriceOutput: ...

    def publish_listing(self, request: PublishListingInput) -> PublishListingOutput: ...

    # Human approval plane (not MCP tools).
    def get_approval(self, approval_id: str) -> ApprovalView | None: ...

    def abandon_approval(self, approval_id: str, reason: str) -> ApprovalView: ...

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        note: str | None = None,
        decided_by: str | None = None,
    ) -> ApprovalView: ...


def create_tool_client(
    settings: Settings, mode: str | None = None
) -> ToolClient:
    selected = (mode or os.environ.get("STOCK_PILOT_TOOL_CLIENT", "inprocess")).strip().lower()
    if selected == "inprocess":
        return StockPilotTools(settings)
    if selected == "mcp":
        return MCPToolClient(settings)
    raise ValueError(
        f"unknown STOCK_PILOT_TOOL_CLIENT={selected!r}; expected 'inprocess' or 'mcp'"
    )


class MCPToolClient:
    """Synchronous facade over a long-lived MCP stdio client session."""

    def __init__(
        self,
        settings: Settings,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        connect_timeout: float = 15.0,
    ) -> None:
        self.settings = settings
        # The orchestrator owns the task/approval DB even when tool calls go
        # over MCP. Make sure the schema exists before the server starts.
        initialize_database(settings.db_path)
        self._executor = ToolExecutor(
            settings, event_sink=make_event_sink(settings.db_path)
        )
        self._call_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="stock-pilot-mcp-client",
            daemon=True,
        )
        self._thread.start()

        self._ready = threading.Event()
        self._session: ClientSession | None = None
        self._error: BaseException | None = None
        self._closed = False

        self._params = StdioServerParameters(
            command=command or sys.executable,
            args=args if args is not None else ["-m", "stock_pilot.mcp_server"],
            env=self._server_env(env),
            cwd=str(PROJECT_ROOT),
        )
        self._task = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(connect_timeout):
            self._error = TimeoutError(
                f"MCP server did not become ready within {connect_timeout:.1f}s"
            )

    # -- lifecycle -------------------------------------------------------

    def _server_env(self, env: dict[str, str] | None) -> dict[str, str]:
        merged = dict(os.environ if env is None else env)
        merged["STOCK_PILOT_DB"] = self.settings.db_path
        merged.setdefault("PYTHONUTF8", "1")
        src = str(PROJECT_ROOT / "src")
        existing = merged.get("PYTHONPATH")
        merged["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
        return merged

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _main(self) -> None:
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self.settings.tool_timeout_seconds,
                ) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surfaced as unavailable
            self._error = exc
            self._ready.set()
        finally:
            self._session = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
            try:
                task.result(timeout=5.0)
            except BaseException:  # noqa: BLE001 - cancellation is expected
                pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
        except RuntimeError:
            pass
        try:
            self._loop.close()
        except RuntimeError:
            pass

    def __enter__(self) -> MCPToolClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- protocol calls --------------------------------------------------

    def _session_or_raise(self) -> ClientSession:
        if self._closed or self._session is None:
            if self._error is None:
                detail = "session is not connected"
            else:
                detail = "".join(
                    traceback.format_exception(
                        type(self._error), self._error, self._error.__traceback__
                    )
                )
            raise ToolUnavailableError(f"MCP server unavailable: {detail}")
        return self._session

    def _content_text(self, result: Any) -> str | None:
        for block in getattr(result, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", None)
        return None

    def _invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        output_type: type[TOutput],
    ) -> TOutput:
        session = self._session_or_raise()
        with self._call_lock:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    session.call_tool(name, arguments), self._loop
                )
            except Exception as exc:  # noqa: BLE001 - transport-level failure
                raise ToolUnavailableError(
                    f"MCP call {name!r} failed: {exc}"
                ) from exc
            # Bound this wait even if the outer executor abandons the worker:
            # a coroutine scheduled on a stopped loop would otherwise block the
            # worker thread forever and hang interpreter shutdown.
            try:
                result = future.result(
                    timeout=self.settings.tool_timeout_seconds + 1.0
                )
            except FuturesTimeoutError as exc:
                future.cancel()
                raise ToolUnavailableError(
                    f"MCP call {name!r} timed out"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - transport-level failure
                raise ToolUnavailableError(
                    f"MCP call {name!r} failed: {exc}"
                ) from exc

        if getattr(result, "is_error", False):
            detail = self._content_text(result) or "tool returned isError"
            raise ToolFailure(ErrorCode.INTERNAL_ERROR, detail)

        structured = getattr(result, "structured_content", None)
        if structured is None:
            text = self._content_text(result)
            if text:
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    structured = None
        if structured is None:
            raise ToolFailure(
                ErrorCode.INTERNAL_ERROR,
                f"MCP tool {name!r} returned no structured content",
            )

        try:
            return output_type.model_validate(structured)
        except Exception as exc:  # noqa: BLE001 - contract violation
            raise ToolFailure(
                ErrorCode.INTERNAL_ERROR,
                f"invalid output from MCP tool {name!r}: {exc}",
            ) from exc

    def _run(
        self,
        name: str,
        request: Any,
        output_type: type[TOutput],
        *,
        is_write: bool,
    ) -> TOutput:
        payload = request.model_dump(mode="json")
        try:
            result = self._executor.call_tool(
                name,
                self._invoke,
                name,
                payload,
                output_type,
                is_write=is_write,
            )
        except ToolFailure as exc:
            return cast(TOutput, output_type.fail(exc.code, exc.message))
        except ToolTimeoutError as exc:
            return cast(TOutput, output_type.fail(ErrorCode.TOOL_TIMEOUT, str(exc)))
        except ToolUnavailableError as exc:
            return cast(
                TOutput, output_type.fail(ErrorCode.TOOL_UNAVAILABLE, str(exc))
            )
        except CircuitOpenError as exc:
            return cast(TOutput, output_type.fail(ErrorCode.CIRCUIT_OPEN, str(exc)))
        except ToolExecutionError as exc:
            return cast(
                TOutput, output_type.fail(ErrorCode.INTERNAL_ERROR, str(exc))
            )
        return cast(TOutput, result)

    # -- four MCP tools --------------------------------------------------

    def check_inventory(self, request: CheckInventoryInput) -> CheckInventoryOutput:
        return self._run(
            "check_inventory", request, CheckInventoryOutput, is_write=False
        )

    def get_listing(self, request: GetListingInput) -> GetListingOutput:
        return self._run("get_listing", request, GetListingOutput, is_write=False)

    def update_price(self, request: UpdatePriceInput) -> UpdatePriceOutput:
        return self._run("update_price", request, UpdatePriceOutput, is_write=True)

    def publish_listing(self, request: PublishListingInput) -> PublishListingOutput:
        return self._run(
            "publish_listing", request, PublishListingOutput, is_write=True
        )

    # -- introspective helper for integration tests ----------------------

    def list_tools(self) -> list[Any]:
        session = self._session_or_raise()
        with self._call_lock:
            try:
                result = asyncio.run_coroutine_threadsafe(
                    session.list_tools(), self._loop
                ).result(timeout=self.settings.tool_timeout_seconds + 1.0)
            except Exception as exc:  # noqa: BLE001 - transport-level failure
                raise ToolUnavailableError(
                    f"MCP list_tools failed: {exc}"
                ) from exc
        return list(result.tools)

    # -- human approval plane (local) ------------------------------------

    def get_approval(self, approval_id: str) -> ApprovalView | None:
        return approval_queue.get_approval(self.settings.db_path, approval_id)

    def abandon_approval(self, approval_id: str, reason: str) -> ApprovalView:
        return approval_queue.abandon_approval(
            self.settings.db_path, approval_id, reason
        )

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        note: str | None = None,
        decided_by: str | None = None,
    ) -> ApprovalView:
        return approval_queue.decide_approval(
            self.settings.db_path, approval_id, decision, note, decided_by
        )
