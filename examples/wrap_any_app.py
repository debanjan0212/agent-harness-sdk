"""Wrap the SDK inside your own application: the pattern Incident Commander follows.

1. Subclass Analysis with your domain fields      (the model output is validated against it)
2. Write domain tools with @tool                   (read-only vs mutating)
3. Add domain rules to the Policy                  (code, not prompt)
4. Pick an Approver that fits your app             (Slack button, ticket, web endpoint, CLI)
5. Listen to events with a hook                    (store a timeline, stream to a UI)
6. Expose it however your app works                (HTTP endpoint, queue worker, cron, CLI)
"""

import asyncio

from pydantic import Field

from agent_harness_sdk import Agent, Analysis, CallbackApprover, Policy, ScriptedModel, setup_telemetry, tool

setup_telemetry("cost-guard", level="WARN")


class CostAnalysis(Analysis):  # 1
    monthly_waste_usd: float = Field(0, ge=0)
    resource: str = ""


@tool()  # 2
def idle_resources(account: str) -> list:
    """Cloud resources with under 2% utilisation for 14 days."""
    return [{"id": "i-0abc", "type": "m5.4xlarge", "cpu_pct": 1.1, "monthly_usd": 560}]


@tool(mutating=True)
def stop_instance(instance_id: str) -> dict:
    """Stop (not terminate) an instance. Reversible."""
    return {"stopped": instance_id}


def no_prod(decision, run):  # 3
    return "never touch prod" if "prod" in str(decision.arguments) else None


def ask_finops(request):  # 4
    print("approval requested: %s %s (%s)" % (request.action, request.arguments, request.rationale))
    return True


timeline = []  # 5


def brain(messages, tools, json_mode):
    s = messages[0]["content"]
    if "Stage READ" in s:
        from agent_harness_sdk import ModelReply, ToolCall

        if any(m["role"] == "tool" for m in messages):
            return "one idle m5.4xlarge"
        return ModelReply(None, [ToolCall("1", "idle_resources", {"account": "dev"})])
    if "Stage ANALYSE" in s:
        return {"summary": "idle dev instance", "monthly_waste_usd": 560, "resource": "i-0abc", "confidence": 0.95}
    return {"action": "stop_instance", "arguments": {"instance_id": "i-0abc"}, "confidence": 0.9, "risk": "low"}


agent = Agent(
    "cost-guard",
    model=ScriptedModel(brain),
    tools=[idle_resources, stop_instance],
    analysis_schema=CostAnalysis,
    policy=Policy(rules=[no_prod]),
    approver=CallbackApprover(ask_finops, "finops"),
    hooks=[lambda e: timeline.append(e.type)],
)


async def handle_request(account: str) -> dict:  # 6: call this from FastAPI, Celery, a cron job...
    run = await agent.arun("find waste in account " + account)
    return {"waste": run.analysis.monthly_waste_usd, "action": run.outcome.status, "steps": len(timeline)}


print(asyncio.run(handle_request("dev")))
