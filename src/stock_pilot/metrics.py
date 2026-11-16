"""Prometheus metrics derived from authoritative SQLite state.

A fresh registry is built per scrape so multiple app instances (and tests) do
not collide. Labels are bounded: tool, outcome, status. Never task ids.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

from stock_pilot import recovery
from stock_pilot.db import connect

LATENCY_BUCKETS_MS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


class StockPilotCollector:
    def __init__(
        self,
        db_path: str,
        circuit_states: Callable[[], dict[str, bool]],
        tool_client_available: bool,
        *,
        mall_db_path: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.mall_db_path = mall_db_path or db_path
        self.circuit_states = circuit_states
        self.tool_client_available = tool_client_available

    def collect(self):
        conn = connect(self.db_path)
        try:
            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            # In MCP mode both the orchestrator client and the tool server
            # record attempts into the shared demo DB. Only the client-side
            # rows carry a task_id; counting those avoids double-counting.
            event_rows = conn.execute(
                "SELECT tool, outcome, error_code, COUNT(*) AS n,"
                " SUM(latency_ms) AS total_ms"
                " FROM tool_events WHERE task_id IS NOT NULL"
                " GROUP BY tool, outcome, error_code"
            ).fetchall()
            latency_rows = conn.execute(
                "SELECT tool, latency_ms FROM tool_events"
                " WHERE task_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        mall_conn = connect(self.mall_db_path)
        try:
            pending = mall_conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'"
            ).fetchone()["n"]
        finally:
            mall_conn.close()

        yield self._tasks(task_rows)
        yield self._events(event_rows)
        yield self._latency(latency_rows)
        yield self._gauge(
            "stock_pilot_approvals_pending",
            "Approvals awaiting a human decision.",
            pending,
        )
        yield self._gauge(
            "stock_pilot_recovery_issues",
            "Non-terminal tasks or orphaned staging rows needing attention.",
            len(
                recovery.scan(
                    self.db_path, mall_db_path=self.mall_db_path
                )
            ),
        )
        yield self._circuits()
        yield self._gauge(
            "stock_pilot_tool_client_available",
            "1 when the configured tool client can serve calls.",
            1 if self.tool_client_available else 0,
        )

    def _tasks(self, rows) -> CounterMetricFamily:
        family = CounterMetricFamily(
            "stock_pilot_tasks_total",
            "Tasks by current status.",
            labels=["status"],
        )
        for row in rows:
            family.add_metric([row["status"]], row["n"])
        return family

    def _events(self, rows) -> CounterMetricFamily:
        family = CounterMetricFamily(
            "stock_pilot_tool_events_total",
            "Tool attempts by tool, outcome, and error code.",
            labels=["tool", "outcome", "error_code"],
        )
        for row in rows:
            family.add_metric(
                [row["tool"], row["outcome"], row["error_code"] or "none"],
                row["n"],
            )
        return family

    def _latency(self, rows) -> HistogramMetricFamily:
        family = HistogramMetricFamily(
            "stock_pilot_tool_latency_ms",
            "Tool attempt latency in milliseconds.",
            labels=["tool"],
        )
        grouped: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            grouped[row["tool"]].append(int(row["latency_ms"]))

        for tool, values in sorted(grouped.items()):
            values.sort()
            buckets = []
            for bound in LATENCY_BUCKETS_MS:
                count = sum(1 for value in values if value <= bound)
                buckets.append((str(bound), count))
            buckets.append(("+Inf", len(values)))
            family.add_metric([tool], buckets, sum(values))
        return family

    def _circuits(self) -> GaugeMetricFamily:
        family = GaugeMetricFamily(
            "stock_pilot_circuit_open",
            "1 when the tool's circuit breaker is open.",
            labels=["tool"],
        )
        for tool, opened in sorted(self.circuit_states().items()):
            family.add_metric([tool], 1 if opened else 0)
        return family

    def _gauge(self, name: str, help_text: str, value: int) -> GaugeMetricFamily:
        family = GaugeMetricFamily(name, help_text)
        family.add_metric([], value)
        return family


def render_metrics(
    db_path: str,
    *,
    mall_db_path: str | None = None,
    circuit_states: dict[str, bool] | None = None,
    tool_client_available: bool = True,
) -> bytes:
    states = circuit_states or {}
    registry = CollectorRegistry(auto_describe=True)
    registry.register(
        StockPilotCollector(
            db_path,
            lambda: states,
            tool_client_available,
            mall_db_path=mall_db_path,
        )
    )
    return generate_latest(registry)
