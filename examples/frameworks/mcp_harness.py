"""Harness.io two ways: the official Harness MCP server for reading, SDK REST tools for acting.

Needs Docker and HARNESS_API_KEY / HARNESS_DEFAULT_ORG_ID / HARNESS_DEFAULT_PROJECT_ID.
"""

import asyncio

from agent_harness_sdk import Agent, CallbackApprover, DeepSeekModel, setup_telemetry
from agent_harness_sdk.mcp import MCPToolset
from agent_harness_sdk.toolpacks.harness import HARNESS_MCP_READ_ONLY, harness_mcp_server, harness_tools

setup_telemetry("harness-example")


async def main():
    async with MCPToolset(
        harness_mcp_server(), read_only=HARNESS_MCP_READ_ONLY, include=HARNESS_MCP_READ_ONLY
    ) as harness:
        write_tools = [t for t in harness_tools() if t.mutating]  # trigger pipeline, rollback
        agent = Agent(
            "release-guard",
            model=DeepSeekModel(),
            tools=[*harness.tools, *write_tools],
            approver=CallbackApprover(lambda r: input("run %s? [y/N] " % r.action) == "y"),
            instructions="You look after deployments in Harness.",
        )
        run = await agent.arun("The last deploy pipeline failed. Find out why and decide what to do.")
        print(run.status, run.analysis.summary if run.analysis else "")


asyncio.run(main())
