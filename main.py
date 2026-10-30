"""Project script entry point; run without arguments to start the MCP server."""

import sys

from jms_mcp.cli import main as cli_main


def main() -> None:
    cli_main(sys.argv[1:] or ["serve"])


if __name__ == "__main__":
    main()
