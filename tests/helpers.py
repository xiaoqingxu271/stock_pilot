"""Shared helpers for tests that run real HTTP MCP servers in-process."""

from __future__ import annotations

import socket
import threading
import time

import uvicorn


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerHandle:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
            # A client may still hold an MCP stream open; don't wait forever.
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("HTTP MCP server thread died during startup")
            time.sleep(0.02)
        raise RuntimeError("HTTP MCP server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
