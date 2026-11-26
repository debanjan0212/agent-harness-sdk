"""AnthropicModel: the harness's OpenAI-format conversation in, Messages API calls out, and the reply back."""

import json
from types import SimpleNamespace as NS

import pytest

from agent_harness_sdk import Agent, AnthropicModel, AutoApprove, Budget, Policy, model_from_env, tool


class FakeMessages:
    """Stands in for anthropic.AsyncAnthropic().messages: records each request, answers from a list."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def create(self, **body):
        self.requests.append(body)
        return self.replies.pop(0)


def fake_client(*replies):
    return NS(messages=FakeMessages(replies))


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def message(*content, stop="end_turn", inp=10, out=5, cache_read=0):
    usage = NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cache_read, cache_creation_input_tokens=0)
    return NS(content=list(content), usage=usage, model="claude-haiku-5-5", stop_reason=stop)


def test_a_tool_loop_becomes_messages_api_turns():
    system, turns = AnthropicModel.to_messages(
        [
            {"role": "system", "content": "you are on call"},
            {"role": "user", "content": "checkout is failing"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "errors", "arguments": '{"svc": "checkout"}'},
                    },
                    {"id": "t2", "type": "function", "function": {"name": "history", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": '{"error_rate": 0.3}'},
            {"role": "tool", "tool_call_id": "t2", "content": ""},
        ]
    )
    assert system == "you are on call"
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    # an empty text block is refused by the API, so the assistant turn carries only its tool calls
    assert turns[1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "errors", "input": {"svc": "checkout"}},
        {"type": "tool_use", "id": "t2", "name": "history", "input": {}},
    ]
    # both results go back in one user turn, each tied to its call
    assert [b["tool_use_id"] for b in turns[2]["content"]] == ["t1", "t2"]
    assert turns[2]["content"][1]["content"] == "(empty)"


async def test_request_shape_and_reply_parsing():
    client = fake_client(message(text("looking"), tool_use("toolu_1", "errors", {"svc": "checkout"}), stop="tool_use"))
    model = AnthropicModel(client=client)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "errors",
                "description": "Error rate",
                "parameters": {"type": "object", "properties": {"svc": {"type": "string"}}},
            },
        }
    ]
    reply = await model.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}], tools)
    body = client.messages.requests[0]
    assert body["model"] == "claude-haiku-5-5" and body["system"] == "s" and body["max_tokens"] == 2048
    assert body["tools"] == [
        {"name": "errors", "description": "Error rate", "input_schema": tools[0]["function"]["parameters"]}
    ]
    assert "temperature" not in body  # only sent when asked for
    assert reply.content == "looking" and reply.finish_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [("toolu_1", "errors", {"svc": "checkout"})]
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (10, 5)


async def test_json_mode_asks_for_json_and_cached_input_still_counts():
    client = fake_client(message(text('{"ok": true}'), inp=4, cache_read=6))
    reply = await AnthropicModel("claude-sonnet-5-5", client=client, temperature=0.0).complete(
        [{"role": "user", "content": "decide"}], json_mode=True
    )
    body = client.messages.requests[0]
    assert body["system"] == AnthropicModel.JSON_ONLY and body["temperature"] == 0.0
    assert body["model"] == "claude-sonnet-5-5"
    assert json.loads(reply.content) == {"ok": True} and reply.usage.input_tokens == 10


async def test_provider_failures_reach_the_harness_as_model_errors():
    class Broken:
        async def create(self, **body):
            raise RuntimeError("overloaded")

    from agent_harness_sdk import ModelError

    with pytest.raises(ModelError, match="overloaded"):
        await AnthropicModel(client=NS(messages=Broken())).complete([{"role": "user", "content": "x"}])


async def test_a_whole_agent_run_on_claude():
    calls = []

    @tool()
    def errors(service: str) -> dict:
        """Error rate for a service."""
        calls.append(service)
        return {"service": service, "error_rate": 0.3}

    @tool(mutating=True)
    def rollback(service: str) -> dict:
        """Roll a service back to its previous release."""
        return {"rolled_back": service}

    client = fake_client(
        message(tool_use("toolu_1", "errors", {"service": "checkout"}), stop="tool_use"),
        message(text("checkout errors are at 30%")),
        message(text('{"summary": "bad release", "likely_cause": "1.4.0", "confidence": 0.9}')),
        message(
            text(
                '{"action": "rollback", "arguments": {"service": "checkout"}, "rationale": "r", '
                '"confidence": 0.9, "risk": "medium"}'
            )
        ),
    )
    agent = Agent(
        "oncall",
        model=AnthropicModel(client=client),
        tools=[errors, rollback],
        policy=Policy(),
        approver=AutoApprove(),
        budget=Budget(max_read_steps=3),
    )
    run = await agent.arun("checkout is failing")
    assert run.status == "completed" and run.outcome.action == "rollback" and run.outcome.status == "done"
    assert calls == ["checkout"]
    # the second request carries the tool result back, tied to Claude's own tool_use id
    second = client.messages.requests[1]["messages"]
    assert second[-1]["content"][0]["tool_use_id"] == "toolu_1"
    assert run.usage.input_tokens == 40 and run.usage.output_tokens == 20


def test_model_from_env_picks_claude(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_MODEL", "claude-haiku-5-5")
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # never called
    model = model_from_env()
    assert isinstance(model, AnthropicModel) and model.provider == "anthropic" and model.model == "claude-haiku-5-5"


async def test_parses_the_anthropic_packages_own_types():
    anthropic = pytest.importorskip("anthropic")
    real = anthropic.types.Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "toolu_9", "name": "errors", "input": {"service": "checkout"}},
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 18, "output_tokens": 4},
        }
    )
    reply = await AnthropicModel(client=fake_client(real)).complete([{"role": "user", "content": "go"}])
    assert reply.content == "checking" and reply.tool_calls[0].arguments == {"service": "checkout"}
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.finish_reason) == (18, 4, "tool_use")
