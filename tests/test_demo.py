"""The one-command demo launcher actually serves a working console."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

from helpers import free_port, wait_until

from stock_pilot.config import PROJECT_ROOT
from stock_pilot.demo import build_parser, port_open


def test_parser_exposes_the_demo_switches(monkeypatch):
    monkeypatch.setenv("STOCK_PILOT_PORT", "8123")
    monkeypatch.setenv("STOCK_PILOT_DB", "data/custom.db")
    args = build_parser().parse_args([])
    assert args.port == 8123
    assert args.db == "data/custom.db"
    assert args.split is False
    assert args.fresh is False


def test_port_open_sees_a_waiting_server():
    port = free_port()
    assert port_open("127.0.0.1", port, timeout=0.1) is False

    import http.server
    import threading

    handler = http.server.BaseHTTPRequestHandler
    handler.log_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert port_open("127.0.0.1", port, timeout=1.0) is True
    finally:
        server.shutdown()
        server.server_close()


def test_launcher_serves_the_console(tmp_path):
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "stock_pilot.demo",
            "--port",
            str(port),
            "--db",
            str(tmp_path / "demo.db"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def health() -> dict | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ) as response:
                return json.loads(response.read())
        except OSError:
            return None

    try:
        assert wait_until(lambda: health() is not None, timeout=30)
        assert health()["status"] == "ok"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=2
        ) as response:
            assert "StockPilot" in response.read().decode("utf-8")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
