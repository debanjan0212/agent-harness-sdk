"""The four stages as a LangGraph graph that pauses for a human before acting."""

import asyncio
import sys
from pathlib import Path

from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from incident_commander import IncidentCommander  # noqa: E402
from incident_commander.sources import FixtureSource  # noqa: E402

from agent_harness_sdk import setup_telemetry  # noqa: E402
from agent_harness_sdk.adapters.langgraph import build_graph  # noqa: E402

setup_telemetry("langgraph-example", level="WARN")
source = FixtureSource("bad-deploy")
graph = build_graph(IncidentCommander(source=source).agent, approval="interrupt")


async def main():
    cfg = {"configurable": {"thread_id": "inc-1"}}
    state = await graph.ainvoke({"task": "HighErrorRate on frontend", "context": source.alert}, cfg)
    ask = state["__interrupt__"][0].value
    print("graph paused for approval:", ask["action"], ask["arguments"])
    state = await graph.ainvoke(Command(resume={"approved": True, "by": "you"}), cfg)
    print("resumed:", state["outcome"]["action"], state["outcome"]["status"])


asyncio.run(main())
