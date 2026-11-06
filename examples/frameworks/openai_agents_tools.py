"""SDK tools in the OpenAI Agents SDK (mutating ones only with an approver)."""

from agents import Agent

from agent_harness_sdk import CallbackApprover, setup_telemetry, tool
from agent_harness_sdk.adapters.openai_agents import to_openai_agents_tools

setup_telemetry("openai-agents-example")


@tool(mutating=True)
def restart_pod(namespace: str, pod: str) -> dict:
    """Delete a pod so its controller recreates it."""
    return {"restarted": pod}


approver = CallbackApprover(lambda req: input("approve %s %s? [y/N] " % (req.action, req.arguments)) == "y")
agent = Agent(
    name="ops",
    instructions="Fix stuck pods.",
    tools=to_openai_agents_tools([restart_pod], include_mutating=True, approver=approver),
)
print([t.name for t in agent.tools])  # Runner.run(agent, "...") with any model the Agents SDK supports
