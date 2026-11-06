import asyncio
import importlib.util
import json

import pytest
from conftest import ROLLBACK, brain, get_errors, rollback

from agent_harness_sdk import ApprovalResponse, AutoApprove, CallbackApprover, ModelReply, ToolCall


def has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except ModuleNotFoundError:  # parent package missing (llama_index.core)
        return False


@pytest.mark.skipif(not has("mcp"), reason="mcp not installed")
def test_mcp_both_directions():
    from mcp import Client
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    from agent_harness_sdk.mcp import MCPToolset, serve_mcp

    remote = MCPServer("remote")

    @remote.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_pipeline(name: str) -> dict:
        return {"pipeline": name, "stages": 3}

    @remote.tool()
    def delete_pipeline(name: str) -> dict:
        return {"deleted": name}

    async def go():
        async with MCPToolset(remote, prefix="h_") as ts:
            tools = {t.name: t for t in ts.tools}
            out = await tools["h_get_pipeline"].invoke({"name": "deploy"})
            served = serve_mcp([get_errors, rollback])
            async with Client(served) as c:
                listed = {t.name: t.annotations for t in (await c.list_tools()).tools}
                res = await c.call_tool("get_errors", {"service": "x"})
            return tools, out, listed, res

    tools, out, listed, res = asyncio.run(go())
    assert not tools["h_get_pipeline"].mutating and tools["h_delete_pipeline"].mutating
    assert json.loads(out)["stages"] == 3
    assert listed["get_errors"].read_only_hint and listed["rollback"].destructive_hint
    assert "0.33" in res.content[0].text


@pytest.mark.skipif(not has("langchain_core"), reason="langchain not installed")
def test_langchain_tools_and_chat_model():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from agent_harness_sdk import Agent
    from agent_harness_sdk.adapters.langchain import LangChainChatModel, agent_as_tool, to_langchain_tools

    lc = to_langchain_tools([get_errors, rollback])
    assert [t.name for t in lc] == ["get_errors"]  # mutating tools are not exported by default
    assert "0.33" in asyncio.run(lc[0].ainvoke({"service": "checkout"})) and "0.33" in lc[0].invoke({"service": "c"})
    guarded = to_langchain_tools([rollback], include_mutating=True)[0]
    assert asyncio.run(guarded.ainvoke({"service": "a", "version": "1"})).startswith("DENIED")

    class Fake(BaseChatModel):
        """Replies like the scripted brain, through LangChain's interfaces."""

        def bind_tools(self, tools, **kw):
            return self

        @property
        def _llm_type(self):
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            sys_text = messages[0].content
            if "Stage READ" in sys_text and not any(m.type == "tool" for m in messages):
                msg = AIMessage(content="", tool_calls=[{"name": "get_errors", "args": {"service": "c"}, "id": "1"}])
            elif "Stage READ" in sys_text:
                msg = AIMessage(content="done")
            elif "Stage ANALYSE" in sys_text:
                msg = AIMessage(content='{"summary": "bad", "confidence": 0.9}')
            else:
                msg = AIMessage(content=json.dumps(ROLLBACK))
            msg.usage_metadata = {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
            return ChatResult(generations=[ChatGeneration(message=msg)])

    agent = Agent("lc", model=LangChainChatModel(Fake()), tools=[get_errors, rollback], approver=AutoApprove())
    run = agent.run("x")
    assert run.outcome.status == "done" and run.usage.input_tokens > 0
    as_tool = agent_as_tool(agent)
    assert json.loads(asyncio.run(as_tool.ainvoke({"task": "again"})))["result"] == "done"


@pytest.mark.skipif(not has("langgraph"), reason="langgraph not installed")
def test_langgraph_interrupt_for_approval():
    from langgraph.types import Command

    from agent_harness_sdk import Agent
    from agent_harness_sdk.adapters.langgraph import build_graph

    agent = Agent("lg", model=brain(ROLLBACK), tools=[get_errors, rollback])
    graph = build_graph(agent, approval="interrupt")
    cfg = {"configurable": {"thread_id": "t1"}}

    async def go():
        first = await graph.ainvoke({"task": "checkout failing"}, cfg)
        second = await graph.ainvoke(Command(resume={"approved": True, "by": "debanjan"}), cfg)
        return first, second

    first, second = asyncio.run(go())
    assert first["__interrupt__"][0].value["action"] == "rollback"
    assert second["outcome"]["status"] == "done" and second["status"] == "completed"


@pytest.mark.skipif(not has("llama_index.core"), reason="llama-index not installed")
def test_llamaindex_tools():
    from agent_harness_sdk.adapters.llamaindex import to_llamaindex_tools

    t = to_llamaindex_tools([get_errors])[0]
    assert t.metadata.name == "get_errors" and "0.33" in str(asyncio.run(t.acall(service="x")))


@pytest.mark.skipif(not has("agents"), reason="openai-agents not installed")
def test_openai_agents_tools():
    from agent_harness_sdk.adapters.openai_agents import to_openai_agents_tools

    approve = CallbackApprover(lambda r: ApprovalResponse(True, "test"))
    tools = {t.name: t for t in to_openai_agents_tools([get_errors, rollback], include_mutating=True, approver=approve)}
    out = asyncio.run(tools["rollback"].on_invoke_tool(None, json.dumps({"service": "a", "version": "2"})))
    assert '"rolled_back": "a"' in out and tools["get_errors"].params_json_schema["required"] == ["service"]


def test_tool_from_scripted_model_reply_shape():
    reply = ModelReply(None, [ToolCall("1", "get_errors", {"service": "x"})])
    assert reply.tool_calls[0].name == "get_errors"
