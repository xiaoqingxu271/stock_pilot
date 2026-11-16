"""Structured logging setup.

``STOCK_PILOT_LOG_FORMAT=json`` emits one JSON object per line with the same
fields as ``tool_events`` plus the task id from the audit context. Text mode
stays the default for local development.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from stock_pilot.audit import current_task_id

_EXTRA_FIELDS = ("task_id", "tool", "attempt", "outcome", "error_code", "latency_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if "task_id" not in payload:
            task_id = current_task_id()
            if task_id is not None:
                payload["task_id"] = task_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Install a single stderr handler on the root logger."""
    fmt = os.environ.get("STOCK_PILOT_LOG_FORMAT", "text").strip().lower()
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(os.environ.get("STOCK_PILOT_LOG_LEVEL", "INFO").upper())
