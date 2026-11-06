"""Smallest useful agent: two tools, one policy, DeepSeek if DEEPSEEK_API_KEY is set, else a scripted model.

python examples/quickstart.py
"""

import os

from agent_harness_sdk import (
    Agent,
    AutoApprove,
    DeepSeekModel,
    ModelReply,
    ScriptedModel,
    ToolCall,
    setup_telemetry,
    tool,
)

setup_telemetry("quickstart", level="INFO")  # add OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 to ship MELT


@tool()
def disk_usage(host: str) -> dict:
    """Disk usage per mount on a host."""
    return {"host": host, "/var/log": "97%", "/": "41%"}


@tool(mutating=True)
def rotate_logs(host: str) -> dict:
    """Rotate and compress logs on a host. Changes the host, so it needs approval."""
    return {"host": host, "freed_gb": 12.4}


def offline(messages, tools, json_mode):
    stage = messages[0]["content"]
    if "Stage READ" in stage:
        if any(m["role"] == "tool" for m in messages):
            return "/var/log is 97% full on web-1"
        return ModelReply(None, [ToolCall("1", "disk_usage", {"host": "web-1"})])
    if "Stage ANALYSE" in stage:
        return {"summary": "/var/log on web-1 is almost full", "likely_cause": "logs not rotated", "confidence": 0.9}
    return {
        "action": "rotate_logs",
        "arguments": {"host": "web-1"},
        "rationale": "frees space safely",
        "confidence": 0.9,
        "risk": "low",
    }


model = DeepSeekModel() if os.getenv("DEEPSEEK_API_KEY") else ScriptedModel(offline)
agent = Agent(
    "disk-janitor",
    model=model,
    tools=[disk_usage, rotate_logs],
    approver=AutoApprove(),
    instructions="You keep Linux hosts healthy.",
)
run = agent.run("web-1 disk alert: /var/log above 95%")
print(run.status, run.analysis.summary, "->", run.outcome.action, run.outcome.status)
