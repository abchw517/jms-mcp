"""MCP Streamable HTTP and legacy SSE, with authentication on every request."""

import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .client import JumpServerClient
from .config import Settings
from .schema import compile_catalog

logger = logging.getLogger(__name__)


class InboundAuth:
    def __init__(self, app, key: str):
        self.app = app
        self.expected = ("Bearer " + key).encode()
        self.enabled = bool(key)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {"/healthz", "/readyz"}:
            return await self.app(scope, receive, send)
        # A session ID never bypasses authentication. Duplicate credentials fail closed.
        values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        if self.enabled and (len(values) != 1 or not hmac.compare_digest(values[0], self.expected)):
            response = JSONResponse(
                {"error": "Unauthorized MCP client"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


class ASGIEndpoint:
    """Prevent Starlette treating an ASGI handler as a Request -> Response endpoint."""

    def __init__(self, handler):
        self.handler = handler

    async def __call__(self, scope, receive, send):
        await self.handler(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    schema: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
):
    settings = settings or Settings()
    settings.validate_runtime()
    mcp = Server("jms-mcp", version=__version__)
    state = {"ready": False, "catalog": None, "client": None}
    # Shared key auth is mandatory for non-loopback listeners. SDK Host/Origin guards
    # remain enabled, and accept the configured listener names plus local probes.
    # Deployments behind a proxy set allowed host/origin explicitly via configuration.
    security = TransportSecuritySettings(
        allowed_hosts=settings.allowed_hosts_list(),
        allowed_origins=settings.allowed_origins_list(),
    )
    sse = SseServerTransport(settings.base_path + "/messages/", security_settings=security)
    manager = StreamableHTTPSessionManager(
        mcp, stateless=True, json_response=True, security_settings=security
    )

    @mcp.list_tools()
    async def list_tools():
        return state["catalog"].tools

    @mcp.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        # Validate here to keep errors free of potentially sensitive argument values.
        return await state["client"].call(state["catalog"], name, arguments)

    @asynccontextmanager
    async def lifespan(app):
        client = JumpServerClient(settings, transport=transport)
        state["client"] = client
        try:
            doc = schema if schema is not None else await client.schema()
            catalog = compile_catalog(doc, settings)
            state["catalog"] = catalog
            logger.info(
                "tools=%d filtered=%d unsupported=%d",
                len(catalog.tools),
                catalog.filtered,
                len(catalog.skipped),
            )
            for skipped in catalog.skipped:
                logger.warning(
                    "Unsupported operation=%s reason=%s", skipped["operation"], skipped["reason"]
                )
            async with manager.run():
                state["ready"] = True
                yield
        finally:
            state["ready"] = False
            await client.close()

    async def connect_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as (reader, writer):
            await mcp.run(reader, writer, mcp.create_initialization_options())

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def ready(request):
        return JSONResponse({"ready": state["ready"]}, status_code=200 if state["ready"] else 503)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", health),
            Route("/readyz", ready),
            Route(settings.base_path, ASGIEndpoint(connect_sse), methods=["GET"]),
            Mount(settings.base_path + "/messages/", app=sse.handle_post_message),
            Route("/mcp", ASGIEndpoint(manager.handle_request), methods=["GET", "POST", "DELETE"]),
        ],
    )
    app.state.gateway = state
    app.add_middleware(InboundAuth, key=settings.api_key.get_secret_value())
    return app
