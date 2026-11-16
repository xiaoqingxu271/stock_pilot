"""MCP tool facade: validation, timeout policy, structured results.

Business failures are returned as ``ok=False`` outputs. Timeouts and open
circuits are also returned structurally so the orchestrator can apply
"state unknown, stop writing" rules without parsing exceptions.
"""

from __future__ import annotations

import logging
from typing import TypeVar, cast

from stock_pilot import approval, inventory, mall
from stock_pilot.audit import make_event_sink
from stock_pilot.config import Settings
from stock_pilot.db import initialize_database
from stock_pilot.enums import ErrorCode
from stock_pilot.errors import (
    CircuitOpenError,
    ToolExecutionError,
    ToolFailure,
    ToolTimeoutError,
)
from stock_pilot.models import (
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

logger = logging.getLogger(__name__)

TOutput = TypeVar("TOutput", bound=ToolOutput)


class StockPilotTools:
    def __init__(
        self,
        settings: Settings | None = None,
        executor: ToolExecutor | None = None,
        *,
        seed: bool = True,
    ) -> None:
        self.settings = settings or Settings.from_env()
        initialize_database(self.settings.db_path, seed=seed)
        self.executor = executor or ToolExecutor(
            self.settings,
            event_sink=make_event_sink(self.settings.db_path),
        )

    def _run(
        self,
        name: str,
        fn,
        request,
        output_type: type[TOutput],
        *,
        is_write: bool,
    ) -> TOutput:
        try:
            result = self.executor.call_tool(
                name,
                fn,
                self.settings.db_path,
                request,
                is_write=is_write,
            )
        except ToolFailure as exc:
            return cast(TOutput, output_type.fail(exc.code, exc.message))
        except ToolTimeoutError as exc:
            return cast(TOutput, output_type.fail(ErrorCode.TOOL_TIMEOUT, str(exc)))
        except CircuitOpenError as exc:
            return cast(TOutput, output_type.fail(ErrorCode.CIRCUIT_OPEN, str(exc)))
        except ToolExecutionError as exc:
            logger.exception("tool %s failed unexpectedly", name)
            return cast(
                TOutput,
                output_type.fail(ErrorCode.INTERNAL_ERROR, str(exc)),
            )
        return cast(TOutput, result)

    # -- read-only ---------------------------------------------------------

    def check_inventory(self, request: CheckInventoryInput) -> CheckInventoryOutput:
        return self._run(
            "check_inventory",
            inventory.check_inventory,
            request,
            CheckInventoryOutput,
            is_write=False,
        )

    def get_listing(self, request: GetListingInput) -> GetListingOutput:
        return self._run(
            "get_listing",
            mall.get_listing,
            request,
            GetListingOutput,
            is_write=False,
        )

    # -- write, approval-gated --------------------------------------------

    def update_price(self, request: UpdatePriceInput) -> UpdatePriceOutput:
        return self._run(
            "update_price",
            mall.update_price,
            request,
            UpdatePriceOutput,
            is_write=True,
        )

    def publish_listing(self, request: PublishListingInput) -> PublishListingOutput:
        return self._run(
            "publish_listing",
            mall.publish_listing,
            request,
            PublishListingOutput,
            is_write=True,
        )

    # -- health ------------------------------------------------------------

    def is_available(self) -> bool:
        return True

    def circuit_states(self) -> dict[str, bool]:
        return self.executor.circuit_states()

    # -- human approval queue (not MCP tools) -----------------------------

    def decide_approval(self, approval_id, decision, note=None, decided_by=None):
        return approval.decide_approval(
            self.settings.db_path, approval_id, decision, note, decided_by
        )

    def abandon_approval(self, approval_id, reason, abandoned_by=None):
        return approval.abandon_approval(
            self.settings.db_path, approval_id, reason, abandoned_by
        )

    def get_approval(self, approval_id):
        return approval.get_approval(self.settings.db_path, approval_id)

    def list_approvals(self, status=None):
        return approval.list_approvals(self.settings.db_path, status)
