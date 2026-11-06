import asyncio
import json

import httpx
import pytest

from agent_harness_sdk import DeepSeekModel, ModelError, OpenAICompatibleModel
from agent_harness_sdk.toolpacks.harness import HarnessClient, harness_tools
from agent_harness_sdk.toolpacks.observability import loki_tools, notify_tools, prometheus_tools


def chat_response(**message):
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


def test_openai_compatible_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return chat_response(
            content=None,
            tool_calls=[{"id": "t1", "function": {"name": "f", "arguments": '{"a": 1}'}}],
            reasoning_content="thinking...",
        )

    model = OpenAICompatibleModel("m", "https://llm.example/v1", "k", transport=httpx.MockTransport(handler))
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x", "reasoning_content": "old"}]
    reply = asyncio.run(model.complete(msgs, tools=[{"type": "function", "function": {"name": "f"}}], json_mode=True))
    assert seen["url"] == "https://llm.example/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["tool_choice"] == "auto" and seen["body"]["response_format"] == {"type": "json_object"}
    assert all("reasoning_content" not in m for m in seen["body"]["messages"])
    assert (
        reply.tool_calls[0].arguments == {"a": 1}
        and reply.usage.input_tokens == 11
        and reply.reasoning == "thinking..."
    )


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = []

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"}) if len(calls) < 3 else chat_response(content="ok")

    model = OpenAICompatibleModel("m", "https://x/v1", transport=httpx.MockTransport(handler))
    assert asyncio.run(model.complete([{"role": "user", "content": "q"}])).content == "ok" and len(calls) == 3
    bad = OpenAICompatibleModel(
        "m", "https://x/v1", transport=httpx.MockTransport(lambda r: httpx.Response(400, text="no"))
    )
    with pytest.raises(ModelError):
        asyncio.run(bad.complete([{"role": "user", "content": "q"}]))


def test_deepseek_defaults(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return chat_response(content="hi")

    m = DeepSeekModel(api_key="k", thinking=True, transport=httpx.MockTransport(handler))
    asyncio.run(m.complete([{"role": "user", "content": "q"}]))
    assert seen["url"] == "https://api.deepseek.com/chat/completions" and m.provider == "deepseek"
    assert seen["body"]["model"] == "deepseek-flash" and seen["body"]["thinking"] == {"type": "enabled"}


def test_harness_rest_tools_hit_the_documented_endpoints():
    seen = []

    def handler(req: httpx.Request):
        seen.append(
            (
                req.method,
                req.url.path,
                dict(req.url.params),
                req.headers.get("x-api-key"),
                req.headers.get("harness-account"),
                req.content,
            )
        )
        if req.url.path.endswith("/execution/summary"):
            return httpx.Response(200, json={"data": {"content": [{"planExecutionId": "e1", "status": "Success"}]}})
        if "/execution/v2/" in req.url.path:
            return httpx.Response(
                200, json={"data": {"pipelineExecutionSummary": {"status": "Failed", "pipelineIdentifier": "deploy"}}}
            )
        return httpx.Response(200, json={"execution_details": {"execution_id": "e2", "status": "RUNNING"}})

    client = HarnessClient(
        api_key="pat.ACC123.tok456.secret789", org="o", project="p", transport=httpx.MockTransport(handler)
    )
    assert client.account == "ACC123"
    tools = {t.name: t for t in harness_tools(client, rollback_pipeline="rollback")}
    assert not tools["harness_recent_executions"].mutating and tools["harness_rollback"].requires_approval
    asyncio.run(tools["harness_recent_executions"].invoke({"pipeline": "deploy"}))
    status = asyncio.run(tools["harness_execution_status"].invoke({"execution_id": "e1"}))
    run = asyncio.run(tools["harness_rollback"].invoke({"service": "checkout", "to_version": "1.3.2"}))
    assert status["status"] == "Failed" and run["execution_id"] == "e2"
    paths = [s[1] for s in seen]
    assert paths == [
        "/pipeline/api/pipelines/execution/summary",
        "/pipeline/api/pipelines/execution/v2/e1",
        "/v1/orgs/o/projects/p/pipelines/rollback/execute",
    ]
    assert seen[0][2]["accountIdentifier"] == "ACC123" and seen[0][3].startswith("pat.") and seen[2][4] == "ACC123"
    assert b"checkout" in seen[2][5] and b"1.3.2" in seen[2][5]


def test_observability_packs():
    def handler(req):
        if req.url.path == "/api/v1/query":
            return httpx.Response(200, json={"data": {"result": [{"metric": {"service": "a"}, "value": [0, "0.5"]}]}})
        if req.url.path.startswith("/loki"):
            lines = [[str(i), "timeout after %dms id=deadbeef%04d" % (i, i)] for i in range(4)]
            return httpx.Response(200, json={"data": {"result": [{"values": lines}]}})
        return httpx.Response(200)

    t = httpx.MockTransport(handler)
    prom = prometheus_tools("http://p", transport=t)[0]
    loki = loki_tools("http://l", transport=t)[0]
    notify = notify_tools("http://hook", transport=t)[0]
    assert asyncio.run(prom.invoke({"query": "up"}))[0]["value"] == "0.5"
    summary = asyncio.run(loki.invoke({"query": '{app="a"}'}))
    assert summary["top_patterns"][0]["count"] == 4  # numbers and ids masked into one pattern
    assert notify.mutating and not notify.requires_approval and asyncio.run(notify.invoke({"text": "hi"}))["posted"]
