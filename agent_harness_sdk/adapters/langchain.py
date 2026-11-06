"""LangChain, both directions.

to_langchain_tools(tools)      SDK tools -> LangChain StructuredTools (use them in any LangChain agent)
agent_as_tool(agent)           a whole harness agent as one LangChain tool (multi-agent / supervisor setups)
LangChainChatModel(llm)        any LangChain chat model (Anthropic, Bedrock, Vertex, Ollama...) as the SDK's model
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent_harness_sdk.adapters._common import pydantic_args, run_tool, selected
from agent_harness_sdk.agent import Agent
from agent_harness_sdk.approvals import Approver
from agent_harness_sdk.models import ModelReply, ToolCall, Usage
from agent_harness_sdk.tools import Tool


def to_langchain_tools(
    tools: Iterable[Tool], *, include_mutating: bool = False, approver: Approver | None = None
) -> list[StructuredTool]:
    out = []
    for t in selected(list(tools), include_mutating):

        async def call(_t=t, **kwargs):
            return await run_tool(_t, kwargs, framework="langchain", approver=approver)

        def call_sync(_t=t, **kwargs):  # .invoke() outside an event loop
            return asyncio.run(run_tool(_t, kwargs, framework="langchain", approver=approver))

        out.append(
            StructuredTool.from_function(
                func=call_sync, coroutine=call, name=t.name, description=t.description, args_schema=pydantic_args(t)
            )
        )
    return out


def agent_as_tool(agent: Agent, *, name: str | None = None, description: str | None = None) -> StructuredTool:
    async def call(task: str) -> str:
        run = await agent.arun(task)
        return json.dumps(
            {
                "status": run.status,
                "analysis": run.analysis.model_dump() if run.analysis else None,
                "action": run.outcome.action if run.outcome else None,
                "result": run.outcome.status if run.outcome else None,
            },
            default=str,
        )

    return StructuredTool.from_function(
        coroutine=call,
        name=name or agent.name.replace("-", "_"),
        description=description or "Run the %s agent on a task." % agent.name,
    )


def _to_lc(messages: list[dict]) -> list:
    out = []
    for m in messages:
        role, content = m["role"], m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content))
        elif role == "user":
            out.append(HumanMessage(content))
        elif role == "assistant":
            calls = [
                {
                    "name": c["function"]["name"],
                    "args": json.loads(c["function"]["arguments"] or "{}"),
                    "id": c["id"],
                    "type": "tool_call",
                }
                for c in m.get("tool_calls") or []
            ]
            out.append(AIMessage(content=content, tool_calls=calls))
        elif role == "tool":
            out.append(ToolMessage(content=content, tool_call_id=m["tool_call_id"]))
    return out


class LangChainChatModel:
    """Use any LangChain BaseChatModel as the harness model."""

    def __init__(self, llm, provider: str | None = None, model: str | None = None):
        self.llm = llm
        self.provider = provider or getattr(llm, "_llm_type", "langchain")
        self.model = model or getattr(llm, "model_name", None) or getattr(llm, "model", None) or "langchain"

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False
    ) -> ModelReply:
        llm = self.llm.bind_tools(tools) if tools else self.llm
        msg = await llm.ainvoke(_to_lc(messages))
        usage = getattr(msg, "usage_metadata", None) or {}
        calls = [
            ToolCall(c.get("id") or "call_" + str(i), c["name"], c.get("args") or {})
            for i, c in enumerate(getattr(msg, "tool_calls", None) or [])
        ]
        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        return ModelReply(
            content=content,
            tool_calls=calls,
            usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
            model=self.model,
            finish_reason=(getattr(msg, "response_metadata", None) or {}).get("finish_reason", ""),
        )
