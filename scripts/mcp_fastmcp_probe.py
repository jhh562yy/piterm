#!/usr/bin/env python3
"""Drive the FULL FastMCP server over stdio with the official mcp client SDK.

    .venv/Scripts/python.exe scripts/mcp_fastmcp_probe.py [--port 9877]

Spawns ``python -m piterm_qgis_mcp serve``, performs the MCP handshake, lists
tools, calls ``system_ping`` and ``layers_list`` and prints what came back.
Exits non-zero when the handshake or a tool call fails.
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "9877"
if "--port" in sys.argv:
    PORT = sys.argv[sys.argv.index("--port") + 1]


async def main():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception:  # noqa: BLE001 - import layout differs across mcp versions
        from mcp.client.session import ClientSession  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
        StdioServerParameters = None  # type: ignore

    env = dict(os.environ)
    env["PITERM_QGIS_HOST"] = env.get("PITERM_QGIS_HOST", "127.0.0.1")
    env["PITERM_QGIS_PORT"] = PORT
    env["PITERM_QGIS_CLIENT"] = "fastmcp-probe"

    if StdioServerParameters is not None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "piterm_qgis_mcp", "serve"],
            env=env,
            cwd=os.path.join(ROOT, "mcp"),
        )
        transport = stdio_client(params)
    else:  # older API
        transport = stdio_client([sys.executable, "-m", "piterm_qgis_mcp",
                                  "serve"], env=env)  # type: ignore

    async with transport as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_info = getattr(init, "server_info", None) or \
                getattr(init, "serverInfo", None)
            protocol = getattr(init, "protocol_version", None) or \
                getattr(init, "protocolVersion", "?")
            print("initialize ok: %s / protocol %s"
                  % (getattr(server_info, "name", "?"), protocol))
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            print("tools: %d (first: %s)" % (len(names), ", ".join(names[:8])))
            ping = await session.call_tool("system_ping", {})
            print("system_ping ->", ping.content[0].text[:160])
            layers = await session.call_tool("layers_list", {"with_counts": False})
            print("layers_list ->", layers.content[0].text[:240])
            status = await session.call_tool("piterm_status", {})
            print("piterm_status ->", status.content[0].text[:200])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
