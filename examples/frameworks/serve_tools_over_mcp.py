"""Expose your SDK tools to Claude, Cursor or any MCP client: python examples/frameworks/serve_tools_over_mcp.py"""

from agent_harness_sdk import tool
from agent_harness_sdk.mcp import serve_mcp


@tool()
def slo_status(service: str) -> dict:
    """Availability vs SLO for a service this month."""
    return {"service": service, "availability": 99.93, "slo": 99.9}


if __name__ == "__main__":
    serve_mcp([slo_status], name="slo-tools").run()
