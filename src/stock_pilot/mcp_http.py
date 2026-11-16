"""Shared Streamable HTTP plumbing for the split MCP servers."""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from stock_pilot.auth import bearer_matches


class BearerAuthMiddleware:
    """Rejects every HTTP request without the expected bearer token.

    Pure ASGI (not ``BaseHTTPMiddleware``) so MCP's streaming responses are
    passed through untouched.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not bearer_matches(self.token, headers.get("authorization")):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_http_app(
    server: MCPServer, *, token: str | None = None, host: str = "127.0.0.1"
):
    app = server.streamable_http_app(host=host)
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def run_http_server(
    server: MCPServer,
    *,
    port: int,
    token: str | None = None,
    host: str = "127.0.0.1",
) -> None:
    import uvicorn

    uvicorn.run(
        create_http_app(server, token=token, host=host),
        host=host,
        port=port,
        log_level=os.environ.get("STOCK_PILOT_LOG_LEVEL", "info"),
    )
