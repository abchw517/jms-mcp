import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from jms_mcp.server import create_app


def app_for(document, settings):
    return create_app(
        settings,
        schema=document,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"count": 1, "results": [{"name": "test"}]})
        ),
    )


async def test_auth_on_every_endpoint_even_session_id(document, settings):
    app = app_for(document, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).json() == {"ready": True}
        for path in ("/sse", "/sse/messages/?session_id=fake", "/mcp"):
            assert (await client.post(path)).status_code == 401
            assert (
                await client.post(path, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
        result = await client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer inbound-test-key",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert result.status_code == 200
        assert result.json()["result"]["serverInfo"]["name"] == "jms-mcp"


def test_runtime_config_rejects_unsafe_defaults(settings):
    settings.api_key = SecretStr("")
    with pytest.raises(ValueError, match="api_key"):
        settings.validate_runtime()
    settings.allow_unauthenticated = True
    settings.validate_runtime()
    settings.server_host = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback"):
        settings.validate_runtime()


def test_cross_origin_swagger_rejected(settings):
    settings.swagger_url = "https://evil.test/swagger.json"
    with pytest.raises(ValueError, match="same origin"):
        settings.validate_runtime()


@asynccontextmanager
async def running_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise AssertionError("Server exited before startup")
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
async def test_real_sdk_initialize_list_and_call(document, settings, transport):
    async with running_server(app_for(document, settings)) as url:
        headers = {"Authorization": "Bearer inbound-test-key"}

        async def verify(read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"assets_hosts_list", "assets_hosts_read"}
                result = await session.call_tool("assets_hosts_list", {"query": {"limit": 1}})
                assert not result.isError and "test" in result.content[0].text
                invalid = await session.call_tool("assets_hosts_list", {"query": {"limit": "bad"}})
                assert invalid.isError

        if transport == "sse":
            sessions = []

            def direct_client(**kwargs):
                return httpx.AsyncClient(**kwargs, trust_env=False)

            async with sse_client(
                url + "/sse",
                headers=headers,
                httpx_client_factory=direct_client,
                on_session_created=sessions.append,
            ) as (read, write):
                assert sessions
                async with httpx.AsyncClient(trust_env=False) as raw:
                    rejected = await raw.post(
                        url + "/sse/messages/",
                        params={"session_id": sessions[0]},
                        json={"jsonrpc": "2.0", "method": "ping", "id": 99},
                    )
                    assert rejected.status_code == 401
                await verify(read, write)
        else:
            async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
                async with streamable_http_client(url + "/mcp", http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    await verify(read, write)
