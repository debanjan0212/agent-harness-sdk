"""MCP both ways.

Use tools from any MCP server (Harness, GitHub, Grafana, Kubernetes, your own):

    async with MCPToolset(StdioServerParameters(command="docker", args=[...])) as harness:
        agent = Agent("ops", model=..., tools=[*my_tools, *harness.tools])
        await agent.arun("...")

Expose your SDK tools to Claude, Cursor or any MCP client:

    serve_mcp([query_metrics, rollback], name="ops-tools").run()

Tools marked readOnlyHint stay read-only in the harness; everything else is treated as mutating (and so
needs policy + approval) unless you list it in `read_only`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import AsyncExitStack
from typing import Any

from agent_harness_sdk.telemetry import get_logger
from agent_harness_sdk.tools import Tool, ToolRegistry

log = get_logger(__name__)


def _text(result: Any) -> str:
    parts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    out = "\n".join(p for p in parts if p)
    if not out and getattr(result, "structured_content", None) is not None:
        out = json.dumps(result.structured_content, default=str)
    if getattr(result, "is_error", False):
        raise RuntimeError(out or "MCP tool returned an error")
    return out


class MCPToolset:
    """Connect to one MCP server (stdio params, URL, or an in-process server) and expose its tools."""

    def __init__(
        self,
        target: Any,
        *,
        prefix: str = "",
        read_only: Iterable[str] = (),
        mutating: Iterable[str] = (),
        include: Iterable[str] | None = None,
        timeout_s: float = 60.0,
    ):
        self.target, self.prefix, self.timeout_s = target, prefix, timeout_s
        self.read_only, self.force_mutating = set(read_only), set(mutating)
        self.include = set(include) if include is not None else None
        self.tools: list[Tool] = []
        self._stack: AsyncExitStack | None = None
        self.client = None

    async def __aenter__(self) -> MCPToolset:
        from mcp import Client

        self._stack = AsyncExitStack()
        self.client = await self._stack.enter_async_context(Client(self.target))
        for t in (await self.client.list_tools()).tools:
            if self.include is not None and t.name not in self.include:
                continue
            ann = t.annotations
            read_only = (
                bool(ann and ann.read_only_hint) or t.name in self.read_only
            ) and t.name not in self.force_mutating
            self.tools.append(
                Tool(
                    name=self.prefix + t.name,
                    description=t.description or t.name,
                    fn=None,
                    mutating=not read_only,
                    json_schema=t.input_schema or {"type": "object", "properties": {}},
                    timeout_s=self.timeout_s,
                    invoker=self._invoker(t.name),
                    source="mcp",
                    meta={"mcp_tool": t.name},
                )
            )
        log.info("loaded %d MCP tools (%d read-only)", len(self.tools), sum(not t.mutating for t in self.tools))
        return self

    def _invoker(self, remote_name: str):
        async def call(args: dict) -> str:
            return _text(await self.client.call_tool(remote_name, args))

        return call

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()


def serve_mcp(tools: Iterable[Tool] | ToolRegistry, name: str = "agent-harness-tools"):
    """Build an MCP server that exposes Python-defined SDK tools. Call .run() for stdio, or
    .run(transport="streamable-http", host=..., port=...) for HTTP."""
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer(name)
    for t in tools:
        if t.fn is None:
            log.warning("skipping %s: only Python-defined tools can be served", t.name)
            continue
        server.add_tool(
            t.fn,
            name=t.name,
            description=t.description,
            annotations=ToolAnnotations(readOnlyHint=not t.mutating, destructiveHint=t.mutating),
        )
    return server
