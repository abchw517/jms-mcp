"""Startup and offline catalog inspection."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .client import JumpServerClient, UpstreamError
from .config import Settings
from .schema import SchemaError, compile_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="JumpServer 3.10.15 MCP gateway")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Serve Streamable HTTP /mcp and SSE /sse")
    inspect = sub.add_parser("catalog", help="Inspect tools without starting a listener")
    inspect.add_argument("--schema", type=Path, help="Read a local Swagger/OpenAPI JSON")
    inspect.add_argument("--output", type=Path, help="Write catalog JSON to this file")
    args = parser.parse_args()
    try:
        settings = Settings()
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
    except (ValueError, OSError, UpstreamError, SchemaError) as exc:
        # Pydantic errors can embed raw input (including credentials), so only print
        # detailed messages for our explicit schema/upstream diagnostics.
        detail = (
            str(exc)
            if isinstance(exc, (SchemaError, UpstreamError))
            else ("Invalid configuration or local file; check .env and file paths")
        )
        print(f"jms-mcp: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
