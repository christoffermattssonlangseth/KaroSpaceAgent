"""Serve the existing sanitizing tools to Codex or any stdio MCP client.

This starts no model session and performs no authentication. The Claude SDK's
server factory returns a standard MCP Server; only its transport changes here.
Run with `python -m karospace_agent.mcp` or `karospace-agent-mcp`.
"""

from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server

from . import commands
from .tools import build_server

INSTRUCTIONS = (
    "Build KaroSpace viewers with these local tools. Prefer them over direct "
    "shell commands for dataset operations: they return aliased schema, fixed status codes, "
    "and artifact metadata. Never request data values, raw file contents, "
    "coordinates, or sample identifiers. Inspect input and structure before "
    "choosing flags; validate outputs before reporting success. Follow the "
    "repository's build-karospace-viewer skill for the full workflow."
)


async def serve() -> None:
    server = build_server()["instance"]
    options = server.create_initialization_options()
    options.instructions = INSTRUCTIONS

    # stdout belongs exclusively to the MCP protocol. Do not tee subprocess
    # logs there (or expose unsanitized progress via MCP notifications). Each
    # tool returns an allowlisted response when the command completes.
    previous_sink = commands.get_progress_sink()
    commands.set_progress_sink(commands.null_sink)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, options)
    finally:
        commands.set_progress_sink(previous_sink)


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
