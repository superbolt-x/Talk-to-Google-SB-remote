"""AdLoop — MCP server connecting Google Ads + GA4 + codebase."""

__version__ = "0.1.0"


def main() -> None:
    """Entry point for `adloop` console script — starts the HTTP MCP server."""
    from adloop.server import main as _server_main

    _server_main()
