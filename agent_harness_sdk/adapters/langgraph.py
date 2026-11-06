"""LangGraph: the four harness stages as graph nodes, with LangGraph-native human approval.

    graph = build_graph(agent, approval="interrupt")       # needs a checkpointer for interrupts
    cfg = {"configurable": {"thread_id": "inc-42"}}
    state = await graph.ainvoke({"task": "checkout 5xx above 5%"}, cfg)
    if "__interrupt__" in state:                             # paused before a mutating action
        state = await graph.ainvoke(Command(resume={"approved": True, "by": "debanjan"}), cfg)

Graph state only holds JSON-safe data (run id, stage outputs), so any LangGraph checkpointer works.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_harness_sdk.agent import Agent, Run
from agent_harness_sdk.approvals import ApprovalResponse


class HarnessState(TypedDict, total=False):
    task: str
    context: dict
    run_id: str
    analysis: dict
    decision: dict
    outcome: dict
    status: str


class _InterruptApprover:
    async def review(self, request) -> ApprovalResponse:
        answer = interrupt(
            {
                "action": request.action,
                "arguments": request.arguments,
                "rationale": request.rationale,
                "risk": request.risk,
                "summary": request.summary,
            }
        )
        if isinstance(answer, dict):
            return ApprovalResponse(
                bool(answer.get("approved")), answer.get("by", "langgraph"), answer.get("comment", "")
            )
        return ApprovalResponse(bool(answer), "langgraph")


def build_graph(agent: Agent, *, approval: str = "agent", checkpointer: Any = None):
    """approval="agent" uses agent.approver; approval="interrupt" pauses the graph for a human (LangGraph interrupt)."""
    runs: dict[str, Run] = {}

    def current(state: HarnessState) -> Run:
        if state["run_id"] not in runs:
            runs[state["run_id"]] = Run(
                id=state["run_id"], agent=agent, task=state["task"], context=state.get("context") or {}
            )
        return runs[state["run_id"]]

    async def start(state: HarnessState) -> HarnessState:
        return {"run_id": state.get("run_id") or uuid.uuid4().hex[:12], "status": "running"}

    async def read(state: HarnessState) -> HarnessState:
        await agent.read(current(state))
        return {}

    async def analyse(state: HarnessState) -> HarnessState:
        run = current(state)
        await agent.analyse(run)
        return {"analysis": run.analysis.model_dump()}

    async def decide(state: HarnessState) -> HarnessState:
        run = current(state)
        await agent.decide(run)
        return {"decision": run.decision.model_dump()}

    async def act(state: HarnessState) -> HarnessState:
        run = current(state)
        saved = agent.approver
        if approval == "interrupt":
            agent.approver = _InterruptApprover()
        try:
            if not run.actions:  # on resume LangGraph re-runs this node; the action must only run once
                await agent.act(run)
        finally:
            agent.approver = saved
        out = run.outcome
        status = "escalated" if out and out.status in ("escalated", "denied") else "completed"
        return {
            "outcome": {"action": out.action, "status": out.status, "output": out.output[:2000]} if out else {},
            "status": status,
        }

    g = StateGraph(HarnessState)
    for name, fn in (("start", start), ("read", read), ("analyse", analyse), ("decide", decide), ("act", act)):
        g.add_node(name, fn)
    g.add_edge(START, "start")
    g.add_edge("start", "read")
    g.add_edge("read", "analyse")
    g.add_edge("analyse", "decide")
    g.add_edge("decide", "act")
    g.add_edge("act", END)
    if approval == "interrupt" and checkpointer is None:
        checkpointer = InMemorySaver()
    return g.compile(checkpointer=checkpointer)
