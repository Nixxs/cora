"""One-off check: can the mcp 2.x client reach GitHub's hosted MCP server?

Run from the repository root with a read-only fine-grained token in the
environment (not a file), for example in PowerShell:

    $env:GITHUB_TOKEN = "github_pat_..."; python examples/mcp_check/main.py; Remove-Item Env:GITHUB_TOKEN

Connects, lists every tool with whether the server says it is read-only, then
closes. Makes no model requests and calls no tools.
"""

import asyncio
import os
import sys

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

URL = "https://api.githubcopilot.com/mcp/"


async def check(token: str) -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        info = client.server_info
        print(f"Connected to {info.name if info else 'server'} {info.version if info else ''}")
        tools, cursor = [], None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools += page.tools
            cursor = page.next_cursor
            if cursor is None:
                break
        for tool in sorted(tools, key=lambda tool: tool.name):
            # A hint from the server, not a guarantee.
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            summary = (tool.description or "").strip().splitlines()[0][:90] if tool.description else ""
            print(f"{'read ' if read_only else 'WRITE'}  {tool.name:40} {summary}")
        print(f"{len(tools)} tools, {sum(1 for t in tools if t.annotations and t.annotations.read_only_hint)} read-only")


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        sys.exit("Set GITHUB_TOKEN in this terminal first (see the docstring).")
    asyncio.run(check(token))


if __name__ == "__main__":
    main()
