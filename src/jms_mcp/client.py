"""Bounded upstream HTTP client. Credentials never come from MCP arguments."""

import asyncio
import json
import logging
import re
import ssl
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from jsonschema import Draft202012Validator, ValidationError
from mcp.types import CallToolResult, TextContent

from .config import Settings
from .schema import Catalog

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """An intentionally sanitized upstream error."""


def combine_url(root: str, path: str) -> str:
    """Preserve a deployment prefix, without duplicating one emitted by Swagger."""
    parsed = urlsplit(root)
    prefix = parsed.path.rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return root.rstrip("/") + path


def scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


class JumpServerClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        verify = (
            ssl.create_default_context(cafile=settings.ca_bundle)
            if settings.ca_bundle
            else (settings.tls_verify)
        )
        headers = {"Authorization": settings.authorization(), "Accept": "application/json"}
        if settings.jms_org_id:
            headers["X-JMS-ORG"] = settings.jms_org_id
        self.http = httpx.AsyncClient(
            headers=headers,
            verify=verify,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.request_timeout),
            limits=httpx.Limits(
                max_connections=settings.max_concurrency,
                max_keepalive_connections=settings.max_concurrency,
            ),
            trust_env=settings.trust_env,
            transport=transport,
        )
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def close(self) -> None:
        await self.http.aclose()

    async def read(self, method: str, url: str, limit: int, **kwargs) -> bytes:
        try:
            # Bound queueing + connection + reading, not only individual socket reads.
            async with asyncio.timeout(self.settings.request_timeout):
                async with self.semaphore, self.http.stream(method, url, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise UpstreamError(f"JumpServer returned HTTP {response.status_code}")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise UpstreamError("JumpServer response exceeds configured byte limit")
                    return bytes(content)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError(
                "JumpServer request timed out; execution outcome may be unknown"
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "JumpServer transport/TLS error; check connectivity and CA"
            ) from exc

    async def schema(self) -> dict:
        url = self.settings.swagger_url or combine_url(
            self.settings.jumpserver_url, "/api/swagger.json"
        )
        raw = await self.read("GET", url, self.settings.max_schema_bytes)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise UpstreamError("Swagger response is not valid JSON") from exc
        if not isinstance(result, dict):
            raise UpstreamError("Swagger response must be an object")
        return result

    async def call(self, catalog: Catalog, name: str, arguments: dict) -> CallToolResult:
        operation = catalog.operations.get(name)
        if operation is None:
            return self.error("Unknown or disabled tool")
        try:
            Draft202012Validator(operation.tool.inputSchema).validate(arguments)
        except ValidationError as exc:
            location = ".".join(map(str, exc.absolute_path)) or "arguments"
            return self.error(f"Invalid tool input at {location}")
        path = operation.path
        for key, value in arguments.get("path", {}).items():
            text = scalar(value)
            if text in {".", ".."} or any(c in text for c in "\r\n"):
                return self.error("Invalid path parameter")
            path = path.replace("{" + key + "}", quote(text, safe=""))
        if re.search(r"{[^{}]+}", path):
            return self.error("Missing path parameter")
        params = []
        for key, value in arguments.get("query", {}).items():
            parameter = next(
                p for p in operation.parameters if p["in"] == "query" and p["name"] == key
            )
            if isinstance(value, list):
                collection_format = parameter.get("collectionFormat", "multi")
                delimiter = {"csv": ",", "ssv": " ", "pipes": "|", "tsv": "\t"}.get(
                    collection_format
                )
                if delimiter is not None:
                    params.append((key, delimiter.join(scalar(v) for v in value)))
                else:
                    params.extend((key, scalar(v)) for v in value)
            else:
                params.append((key, scalar(value)))
        started = time.monotonic()
        try:
            options = {"params": params}
            if "body" in arguments:
                options["json"] = arguments["body"]
            raw = await self.read(
                operation.method.upper(),
                combine_url(self.settings.jumpserver_url, path),
                self.settings.max_response_bytes,
                **options,
            )
            if not raw:
                result = "{}"
            else:
                try:
                    result = json.dumps(json.loads(raw), ensure_ascii=False)
                except (ValueError, UnicodeError):
                    result = raw.decode("utf-8", errors="replace")
            logger.info(
                "tool=%s method=%s result=ok duration_ms=%.0f",
                name,
                operation.method,
                (time.monotonic() - started) * 1000,
            )
            return CallToolResult(content=[TextContent(type="text", text=result)])
        except UpstreamError as exc:
            logger.warning(
                "tool=%s method=%s result=error duration_ms=%.0f",
                name,
                operation.method,
                (time.monotonic() - started) * 1000,
            )
            return self.error(str(exc))

    @staticmethod
    def error(message: str) -> CallToolResult:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
