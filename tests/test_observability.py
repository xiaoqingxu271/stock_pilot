"""Metrics and structured logs derived from the same tool/task state."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from stock_pilot import audit
from stock_pilot.api import create_app
from stock_pilot.config import Settings
from stock_pilot.db import initialize_database
from stock_pilot.logging_config import JsonFormatter, configure_logging
from stock_pilot.models import CheckInventoryInput


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _payload(price: str = "59.00") -> dict:
    return {
        "sku_id": "SKU-TSHIRT-001",
        "spec": "白色-M",
        "target_price": price,
        "reason": "观测性测试",
    }


def test_metrics_reflect_a_verified_task(client):
    task = client.post("/tasks", json=_payload()).json()
    decision = client.post(
        f"/approvals/{task['approval_id']}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "verified"

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text

    assert 'stock_pilot_tasks_total{status="verified"} 1.0' in body
    assert "stock_pilot_approvals_pending 0.0" in body
    assert "stock_pilot_tool_client_available 1.0" in body
    assert "stock_pilot_recovery_issues 0.0" in body
    assert (
        'stock_pilot_tool_events_total{error_code="none",outcome="ok",'
        'tool="publish_listing"} 1.0'
    ) in body
    assert 'stock_pilot_tool_latency_ms_count{tool="publish_listing"} 1.0' in body
    assert 'stock_pilot_circuit_open{tool="publish_listing"} 0.0' in body


class _DownToolClient:
    """Minimal stand-in: DB is fine, transport and one circuit are not."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_available(self) -> bool:
        return False

    def circuit_states(self) -> dict[str, bool]:
        return {"publish_listing": True}

    def check_inventory(self, request):  # pragma: no cover - never called
        raise AssertionError("tool call attempted in a metrics-only test")


def test_metrics_and_healthz_report_degradation(settings: Settings):
    initialize_database(settings.db_path)
    app = create_app(settings, tool_client=_DownToolClient(settings))
    with TestClient(app) as client:
        body = client.get("/metrics").text
        assert 'stock_pilot_circuit_open{tool="publish_listing"} 1.0' in body
        assert "stock_pilot_tool_client_available 0.0" in body

        health = client.get("/healthz")
        assert health.status_code == 503
        assert health.json()["status"] == "degraded"
        assert health.json()["tool_client"] is False


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "stock_pilot.runner", logging.INFO, __file__, 1, "tool attempt", None, None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_task_and_attempt_fields():
    with audit.task_context("task-json-1"):
        line = JsonFormatter().format(
            _record(
                tool="check_inventory",
                attempt=1,
                outcome="ok",
                latency_ms=7,
            )
        )

    payload = json.loads(line)
    assert payload["task_id"] == "task-json-1"
    assert payload["logger"] == "stock_pilot.runner"
    assert payload["message"] == "tool attempt"
    assert payload["tool"] == "check_inventory"
    assert payload["attempt"] == 1
    assert payload["outcome"] == "ok"
    assert payload["latency_ms"] == 7
    assert "error_code" not in payload


def test_json_formatter_whitelists_fields():
    line = JsonFormatter().format(
        _record(
            tool="update_price",
            authorization="Bearer super-secret",
            headers={"Authorization": "Bearer super-secret"},
        )
    )
    payload = json.loads(line)
    assert payload["tool"] == "update_price"
    assert "authorization" not in payload
    assert "headers" not in payload
    assert "super-secret" not in line


def test_tool_attempts_log_with_task_context(tools, settings: Settings):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    runner_logger = logging.getLogger("stock_pilot.runner")
    previous_level = runner_logger.level
    previous_propagate = runner_logger.propagate
    runner_logger.addHandler(handler)
    runner_logger.setLevel(logging.INFO)
    runner_logger.propagate = False
    try:
        with audit.task_context("task-log-1"):
            result = tools.check_inventory(
                CheckInventoryInput(sku_id="SKU-TSHIRT-001", spec="白色-M")
            )
    finally:
        runner_logger.removeHandler(handler)
        runner_logger.setLevel(previous_level)
        runner_logger.propagate = previous_propagate

    assert result.ok
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["task_id"] == "task-log-1"
    assert payload["tool"] == "check_inventory"
    assert payload["outcome"] == "ok"
    assert isinstance(payload["latency_ms"], int)


def test_configure_logging_emits_one_json_object(monkeypatch, capsys):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setenv("STOCK_PILOT_LOG_FORMAT", "json")
    monkeypatch.setenv("STOCK_PILOT_LOG_LEVEL", "INFO")
    try:
        configure_logging()
        with audit.task_context("task-config-log"):
            logging.getLogger("stock_pilot.test").warning("hello")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "WARNING"
        assert payload["message"] == "hello"
        assert payload["task_id"] == "task-config-log"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
