"""Expose the official ElevenLabs MCP server for Streamable HTTP mounting."""

import os

from mcp.server.fastmcp import FastMCP


# ELEVENLABS MCP
def get_elevenlabs_mcp() -> FastMCP:
    """Return the ElevenLabs FastMCP instance configured for stateless HTTP mounting."""

    if not os.environ.get("ELEVENLABS_API_KEY", "").strip():
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Configure it in the deployment environment."
        )

    from elevenlabs_mcp.server import mcp

    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = "/"
    return mcp
