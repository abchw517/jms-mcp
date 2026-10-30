"""Startup and offline catalog inspection."""

import argparse
import asyncio
import json
import logging
import ssl
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError
from pydantic_settings import SettingsError

from . import __version__
from .client import JumpServerClient, UpstreamError
from .config import ConfigurationError, Settings
from .schema import SchemaError, compile_catalog


def error_detail(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        # Do not serialize input, context, or arbitrary validator messages.
        fields = [
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        return "Invalid configuration fields: " + "; ".join(fields)
    if isinstance(exc, (ConfigurationError, SchemaError, UpstreamError)):
        return str(exc)
    if isinstance(exc, OSError):
        return f"Local file error ({type(exc).__name__}); check paths, mounts and permissions"
    return f"Invalid configuration or local input ({type(exc).__name__}); check configuration"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="JumpServer 3.10.15 MCP gateway")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Serve Streamable HTTP /mcp and SSE /sse")
    sub.add_parser(
        "check-config", help="Validate local configuration without contacting JumpServer"
    )
    inspect = sub.add_parser("catalog", help="Inspect tools without starting a listener")
    inspect.add_argument("--schema", type=Path, help="Read a local Swagger/OpenAPI JSON")
    inspect.add_argument("--output", type=Path, help="Write catalog JSON to this file")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if args.command == "check-config":
            settings.validate_runtime()
            if settings.ca_bundle:
                try:
                    ssl.create_default_context(cafile=settings.ca_bundle)
                except (OSError, ssl.SSLError) as exc:
                    raise ConfigurationError(
                        "ca_bundle cannot be loaded; check the container mount, permissions and PEM"
                    ) from exc
            print(
                "Configuration OK (offline; JumpServer connectivity and token validity not checked)"
            )
            return
        # Avoid HTTP SDK debug logs that might include URLs or transport metadata.
        logging.basicConfig(level=settings.log_level.upper(), format="%(levelname)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.WARNING)
        if args.command == "serve":
            from .server import create_app

            app = create_app(settings)
            uvicorn.run(
                app,
                host=settings.server_host,
                port=settings.server_port,
                log_level=settings.log_level.lower(),
                access_log=False,
            )
            return

        async def document():
            if args.schema:
                if args.schema.stat().st_size > settings.max_schema_bytes:
                    raise SchemaError("Local schema exceeds max_schema_bytes")
                return json.loads(args.schema.read_text())
            settings.validate_runtime()
            client = JumpServerClient(settings)
            try:
                return await client.schema()
            finally:
                await client.close()

        catalog = compile_catalog(asyncio.run(document()), settings)
        output = json.dumps(
            {
                "version": __version__,
                "tool_count": len(catalog.tools),
                "filtered": catalog.filtered,
                "skipped": catalog.skipped,
                "tools": [t.model_dump(exclude_none=True) for t in catalog.tools],
            },
            ensure_ascii=False,
            indent=2,
        )
        if args.output:
            args.output.write_text(output + "\n")
        else:
            print(output)
    except (ValueError, OSError, UpstreamError, SettingsError) as exc:
        print(f"jms-mcp: {error_detail(exc)}", file=sys.stderr)
        raise SystemExit(1) from None
