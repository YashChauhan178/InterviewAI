from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client


MCP_SERVER_PATH = Path(__file__).with_name("mcp_server.py")


@asynccontextmanager
async def connect_mcp_server() -> AsyncGenerator[ClientSession, None]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER_PATH)],
        cwd=str(MCP_SERVER_PATH.parent),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session
