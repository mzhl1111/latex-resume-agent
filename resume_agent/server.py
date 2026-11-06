"""One HTTP app serving both protocols.

    /mcp                              MCP (streamable HTTP)
    /a2a                              A2A JSON-RPC
    /.well-known/agent-card.json      A2A agent card
    /healthz                          liveness

Env:
    RESUME_AGENT_TOKEN   if set, /mcp and /a2a require "Authorization: Bearer <token>"
    PUBLIC_BASE_URL      URL advertised in the agent card (default: from request)
    ANTHROPIC_API_KEY    enables generate_resume / A2A generation
    RESUME_JOBS_DIR      where job workspaces are written (mount a volume here)
"""

from __future__ import annotations

import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .a2a_server import agent_card, make_handler
from .compile import check_dependencies
from .mcp_server import mcp

PUBLIC_PATHS = ("/healthz", "/.well-known/")


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and not scope["path"].startswith(PUBLIC_PATHS):
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(got, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(*, token: str | None = None, generate=None):
    os.environ.setdefault("RESUME_TRACE_MODE", "server")
    token = token if token is not None else os.environ.get("RESUME_AGENT_TOKEN") or None
    # host="0.0.0.0" turns off the localhost-only DNS-rebinding guard; auth is the bearer token instead.
    app = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0", stateless_http=True)

    async def card(request: Request):
        base = os.environ.get("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")
        return JSONResponse(agent_card(base, auth=bool(token)))

    async def health(_request: Request):
        missing = check_dependencies()
        return JSONResponse(
            {"ok": not missing, "missing": missing, "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))},
            status_code=200 if not missing else 503,
        )

    app.router.routes[0:0] = [
        Route("/healthz", health),
        Route("/.well-known/agent-card.json", card),
        Route("/.well-known/agent.json", card),
        Route("/a2a", make_handler(generate), methods=["POST"]),
    ]
    if token:
        app.add_middleware(BearerAuth, token=token)
    return app


def run(host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
