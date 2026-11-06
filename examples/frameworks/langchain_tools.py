"""Use SDK tools inside a LangChain agent, and a whole harness agent as one LangChain tool."""

from agent_harness_sdk import get_logger, setup_telemetry, tool
from agent_harness_sdk.adapters.langchain import to_langchain_tools

setup_telemetry("langchain-example")
log = get_logger(__name__)


@tool()
def pod_restarts(namespace: str) -> dict:
    """Pods that restarted in the last hour."""
    return {"checkout-7d9f": 7}


lc_tools = to_langchain_tools([pod_restarts])  # each call still gets a span, metrics and secret redaction
log.info("exported %d tools to LangChain: %s", len(lc_tools), [t.name for t in lc_tools])
# from langchain.agents import create_agent; create_agent(model, tools=lc_tools)  # any LangChain agent
print(lc_tools[0].invoke({"namespace": "shop"}))
