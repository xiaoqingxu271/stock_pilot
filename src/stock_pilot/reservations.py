"""Stock-reservation sweep.

The publish saga releases its reservation best-effort. A crash between
``reserve_inventory`` and ``release_inventory`` leaves the row reserved, which
blocks every later proposal for that inventory record. The TTL is advisory:
the sweep only releases reservations whose approval can no longer publish, so
it can never steal stock from a task that is about to commit.

No new durable state is introduced. The sweep crosses stores through the same
tool boundary as the graph: reservations come from the inventory server, the
approval verdict from the local approval plane, and task state from the
orchestrator store.
"""

from __future__ import annotations

import logging
import threading

from stock_pilot import tasks
from stock_pilot.clients import ToolClient
from stock_pilot.db import utcnow
from stock_pilot.enums import (
    TERMINAL_TASK_STATUSES,
    ApprovalStatus,
    ErrorCode,
    ReservationGcAction,
    TaskStatus,
)
from stock_pilot.errors import ToolFailure
from stock_pilot.models import (
    ReleaseInventoryInput,
    ReservationGcItem,
    ReservationGcResponse,
    ReservationView,
)

logger = logging.getLogger(__name__)

_RELEASABLE_APPROVALS = frozenset(
    {
        ApprovalStatus.CONSUMED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.ABANDONED,
    }
)


def disposition(
    approval_status: ApprovalStatus | None,
    task_status: TaskStatus | None,
    age_seconds: int,
    ttl_seconds: int,
) -> tuple[ReservationGcAction, str]:
    """Decide what to do with one reservation. Conservative by construction."""
    if approval_status is None:
        return (
            ReservationGcAction.RELEASED,
            "审批记录缺失，预留不可能再被消费。",
        )
    if approval_status in _RELEASABLE_APPROVALS:
        return (
            ReservationGcAction.RELEASED,
            f"审批已是 {approval_status.value}，预留不再需要。",
        )
    if approval_status is ApprovalStatus.PENDING:
        # A reservation is only created after approval, so this is anomalous.
        # It still cannot publish while pending, but leave it for a human
        # rather than silently changing state the saga never produced.
        return (
            ReservationGcAction.STALE,
            "审批仍待决但存在预留，状态异常，需人工核查。",
        )

    # Approved: the proposal may still publish.
    if task_status is None:
        return (
            ReservationGcAction.RELEASED,
            "审批已通过但没有关联任务，预留不可能再被消费。",
        )
    if task_status in TERMINAL_TASK_STATUSES:
        return (
            ReservationGcAction.RELEASED,
            f"任务已终态（{task_status.value}），预留不再需要。",
        )
    if age_seconds > ttl_seconds:
        return (
            ReservationGcAction.STALE,
            f"预留已存活 {age_seconds}s，超过 TTL {ttl_seconds}s，"
            "但审批仍可发布；不自动释放。",
        )
    return (
        ReservationGcAction.RETAINED,
        "审批可发布且任务仍活跃，保留预留。",
    )


def sweep(
    client: ToolClient, *, ttl_seconds: int | None = None
) -> ReservationGcResponse:
    """Release only reservations whose approval/task can no longer publish."""
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else client.settings.reservation_ttl_seconds
    )
    listed = client.list_reservations()
    if not listed.ok:
        raise ToolFailure(
            listed.error_code or ErrorCode.INTERNAL_ERROR,
            listed.message or "cannot read stock reservations",
        )

    items: list[ReservationGcItem] = []
    for reservation in listed.reservations:
        items.append(_sweep_one(client, reservation, ttl))

    return ReservationGcResponse(
        checked_at=utcnow(),
        released=sum(
            item.action is ReservationGcAction.RELEASED for item in items
        ),
        retained=sum(
            item.action is ReservationGcAction.RETAINED for item in items
        ),
        stale=sum(item.action is ReservationGcAction.STALE for item in items),
        failed=sum(item.action is ReservationGcAction.FAILED for item in items),
        items=items,
    )


def _sweep_one(
    client: ToolClient, reservation: ReservationView, ttl_seconds: int
) -> ReservationGcItem:
    approval = client.get_approval(reservation.approval_id)
    task = tasks.find_task_by_approval(
        client.settings.db_path, reservation.approval_id
    )
    task_status = task.status if task is not None else None
    approval_status = approval.status if approval is not None else None

    action, reason = disposition(
        approval_status,
        task_status,
        reservation.age_seconds,
        ttl_seconds,
    )
    if action is ReservationGcAction.RELEASED:
        released = client.release_inventory(
            ReleaseInventoryInput(approval_id=reservation.approval_id)
        )
        if not released.ok or not released.released:
            code = released.error_code or ErrorCode.INTERNAL_ERROR
            action = ReservationGcAction.FAILED
            reason = f"释放失败：{code.value}；可重试人工清理。"

    return ReservationGcItem(
        inventory_record_id=reservation.inventory_record_id,
        approval_id=reservation.approval_id,
        sku_id=reservation.sku_id,
        spec=reservation.spec,
        approval_status=approval_status,
        task_status=task_status,
        age_seconds=reservation.age_seconds,
        action=action,
        reason=reason,
    )


class ReservationSweeper:
    """Opt-in periodic sweep that uses the exact operator-triggered rule.

    Disabled by default (``STOCK_PILOT_RESERVATION_GC_INTERVAL=0``). The timer
    only runs ``sweep()``; it cannot release a reservation whose approval can
    still publish, so it does not weaken the saga's stock guarantee. It is a
    convenience for long-running deployments, not a force-expiry mechanism.
    """

    def __init__(
        self,
        client: ToolClient,
        *,
        interval_seconds: float,
        ttl_seconds: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.client = client
        self.interval_seconds = interval_seconds
        self.ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="stock-pilot-reservation-gc",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None

    def run_once(self) -> ReservationGcResponse:
        result = sweep(self.client, ttl_seconds=self.ttl_seconds)
        if result.released or result.failed:
            logger.info(
                "reservation sweep released=%d retained=%d stale=%d failed=%d",
                result.released,
                result.retained,
                result.stale,
                result.failed,
            )
        return result

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a failed pass retries next tick
                logger.exception("reservation sweep pass failed")

    def __enter__(self) -> ReservationSweeper:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
