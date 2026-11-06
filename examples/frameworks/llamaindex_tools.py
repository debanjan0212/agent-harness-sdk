"""SDK tools as LlamaIndex FunctionTools."""

import asyncio

from agent_harness_sdk import setup_telemetry, tool
from agent_harness_sdk.adapters.llamaindex import to_llamaindex_tools

setup_telemetry("llamaindex-example")


@tool()
def runbook(alert: str) -> str:
    """Fetch the runbook section for an alert."""
    return "HighErrorRate: check recent deploys first, then dependencies."


tools = to_llamaindex_tools([runbook])  # pass to FunctionAgent(tools=tools, llm=...)
print(asyncio.run(tools[0].acall(alert="HighErrorRate")))
