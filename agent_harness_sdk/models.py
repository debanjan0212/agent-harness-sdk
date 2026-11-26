"""Model providers. Every provider returns the same ModelReply, so the harness never cares which one runs.

DeepSeekModel        default. DeepSeek's OpenAI-compatible API (https://api.deepseek.com).
OpenAICompatibleModel OpenAI, Azure OpenAI v1, OpenRouter, Together, Groq, vLLM, Ollama, LiteLLM, ...
AnthropicModel       Claude over Anthropic's Messages API (pip install agent-harness-sdk[anthropic]).
ScriptedModel        no network: replies come from a function or a list. For tests, demos, air-gapped runs.
LangChainChatModel   any LangChain chat model (Anthropic, Bedrock, Vertex, ...): see adapters/langchain.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from agent_harness_sdk.telemetry import get_logger

log = get_logger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    reasoning: str | None = None  # DeepSeek thinking output; never sent back to the API


class ChatModel(Protocol):
    provider: str
    model: str

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False
    ) -> ModelReply: ...


class ModelError(Exception):
    pass


class OpenAICompatibleModel:
    provider = "openai"
    RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        *,
        temperature: float = 0.0,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        extra_body: dict | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.temperature, self.max_retries, self.extra_body = temperature, max_retries, extra_body or {}
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    def _payload(self, messages: list[dict], tools: list[dict] | None, json_mode: bool) -> dict:
        # reasoning_content must not be sent back (DeepSeek rejects it); other providers ignore our extra keys
        clean = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
        body = {"model": self.model, "messages": clean, "temperature": self.temperature}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)
        return body

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False
    ) -> ModelReply:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        url = self.base_url + "/chat/completions"
        body = self._payload(messages, tools, json_mode)
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(url, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise ModelError("%s unreachable: %s" % (self.provider, exc)) from exc
                await self._backoff(attempt, str(exc))
                continue
            if resp.status_code in self.RETRY_STATUS and attempt < self.max_retries:
                await self._backoff(attempt, "HTTP %s" % resp.status_code, resp.headers.get("retry-after"))
                continue
            if resp.status_code >= 400:
                raise ModelError("%s returned HTTP %s: %s" % (self.provider, resp.status_code, resp.text[:500]))
            return self._parse(resp.json())
        raise ModelError("unreachable")

    async def _backoff(self, attempt: int, why: str, retry_after: str | None = None) -> None:
        delay = (
            float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * 2**attempt + random.random() * 0.25
        )
        log.warning("model call retry %d in %.2fs: %s", attempt + 1, delay, why, gen_ai_provider=self.provider)
        await asyncio.sleep(delay)

    def _parse(self, data: dict) -> ModelReply:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {"_raw": raw}  # the harness reports this back to the model as a tool error
            calls.append(ToolCall(c.get("id") or ("call_" + str(len(calls))), c["function"]["name"], args))
        u = data.get("usage") or {}
        return ModelReply(
            content=msg.get("content"),
            tool_calls=calls,
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason") or "",
            reasoning=msg.get("reasoning_content"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class DeepSeekModel(OpenAICompatibleModel):
    """DeepSeek over its OpenAI-compatible API.

    Model id comes from DEEPSEEK_MODEL (default "deepseek-flash"). The legacy ids deepseek-chat and
    deepseek-reasoner were retired on 24 July 2026. thinking=True sends {"thinking": {"type": "enabled"}}.
    """

    provider = "deepseek"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        thinking: bool | None = None,
        **kwargs: Any,
    ):
        extra = dict(kwargs.pop("extra_body", None) or {})
        if thinking is not None:
            extra["thinking"] = {"type": "enabled" if thinking else "disabled"}
        super().__init__(
            model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key or os.getenv("DEEPSEEK_API_KEY"),
            extra_body=extra,
            **kwargs,
        )
        if not self.api_key:
            log.warning("DEEPSEEK_API_KEY is not set: calls will fail until it is", gen_ai_provider="deepseek")


class AnthropicModel:
    """Claude over Anthropic's Messages API, through the official `anthropic` package.

    Credentials are whatever that package finds: ANTHROPIC_API_KEY, or workload identity federation
    (ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID, ANTHROPIC_SERVICE_ACCOUNT_ID and
    ANTHROPIC_IDENTITY_TOKEN_FILE), so a CI job can call Claude with no stored key at all.

    The harness speaks the OpenAI chat format; this class turns it into Messages API calls and back. System
    messages become the system prompt, tool results become tool_result blocks in a user turn, and json_mode asks
    for one JSON object in the system prompt, because the Messages API has no response_format switch.
    temperature is only sent when you set it.
    """

    provider = "anthropic"
    JSON_ONLY = "Reply with one JSON object and nothing else: no prose and no code fence around it."

    def __init__(
        self,
        model: str | None = None,
        *,
        max_tokens: int = 2048,
        temperature: float | None = None,
        client: Any = None,
        **client_kwargs: Any,
    ):
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
        self.max_tokens, self.temperature = max_tokens, temperature
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                need = "AnthropicModel needs the anthropic package: pip install agent-harness-sdk[anthropic]"
                raise ModelError(need) from exc
            client = anthropic.AsyncAnthropic(**client_kwargs)
        self._client = client

    @staticmethod
    def to_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """OpenAI-format chat messages -> (system prompt, Messages API turns). Neighbouring turns from the same
        role are merged, so several tool results land in one user turn, as the API wants."""
        system, turns = [], []

        def add(role: str, blocks: list[dict]) -> None:
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"].extend(blocks)
            else:
                turns.append({"role": role, "content": blocks})

        for m in messages:
            role, text = m.get("role"), m.get("content") or ""
            if role == "system":
                if text:
                    system.append(text)
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": text or "(empty)"}
                add("user", [block])
            elif role == "assistant":
                blocks = [{"type": "text", "text": text}] if text.strip() else []
                for c in m.get("tool_calls") or []:
                    raw = c["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        args = {"_raw": raw}
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
                add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
            else:
                add("user", [{"type": "text", "text": text or "(empty)"}])
        return "\n\n".join(system), turns

    @staticmethod
    def to_tools(tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            fn = t.get("function", t)
            schema = fn.get("parameters") or {"type": "object", "properties": {}}
            out.append({"name": fn["name"], "description": fn.get("description") or fn["name"], "input_schema": schema})
        return out

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False
    ) -> ModelReply:
        system, turns = self.to_messages(messages)
        if json_mode:
            system = (system + "\n\n" + self.JSON_ONLY).strip()
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": turns}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = self.to_tools(tools)
        if self.temperature is not None:
            body["temperature"] = self.temperature
        try:
            resp = await self._client.messages.create(**body)
        except Exception as exc:  # noqa: BLE001 - every provider failure reaches the harness as a ModelError
            raise ModelError("anthropic call failed: %s: %s" % (type(exc).__name__, str(exc)[:500])) from exc
        texts, calls = [], []
        for block in resp.content or []:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        u = resp.usage
        # cache reads and writes are input too, and are billed; count them so budgets stay honest
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        return ModelReply(
            content="\n".join(texts) if texts else None,
            tool_calls=calls,
            usage=Usage((u.input_tokens or 0) + cached, u.output_tokens or 0),
            model=getattr(resp, "model", "") or self.model,
            finish_reason=getattr(resp, "stop_reason", "") or "",
        )

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            out = close()
            if asyncio.iscoroutine(out):
                await out


class ScriptedModel:
    """Deterministic model. `script` is either a list of replies (str, dict or ModelReply) used in order,
    or a function(messages, tools, json_mode) -> reply. Dicts are returned as JSON content."""

    provider = "scripted"

    def __init__(self, script: list | Callable[[list[dict], list[dict] | None, bool], Any], model: str = "scripted"):
        self.script, self.model, self.calls = script, model, 0

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False
    ) -> ModelReply:
        self.calls += 1
        if callable(self.script):
            out = self.script(messages, tools, json_mode)
            if asyncio.iscoroutine(out):
                out = await out
        else:
            if not self.script:
                raise ModelError("scripted model ran out of replies")
            out = self.script.pop(0)
        if isinstance(out, ModelReply):
            return out
        if isinstance(out, dict | list):
            out = json.dumps(out)
        return ModelReply(
            content=str(out),
            model=self.model,
            finish_reason="stop",
            usage=Usage(sum(len(str(m.get("content") or "")) for m in messages) // 4, len(str(out)) // 4),
        )


def model_from_env() -> ChatModel:
    """AGENT_MODEL_PROVIDER=deepseek (default) | anthropic | openai. OpenAI-compatible endpoints read
    AGENT_MODEL_BASE_URL, AGENT_MODEL and AGENT_MODEL_API_KEY. anthropic reads AGENT_MODEL (default
    claude-haiku-5-5) and finds its own credentials."""
    provider = os.getenv("AGENT_MODEL_PROVIDER", "deepseek")
    if provider == "deepseek":
        return DeepSeekModel()
    if provider == "anthropic":
        return AnthropicModel(os.getenv("AGENT_MODEL") or None)
    return OpenAICompatibleModel(
        os.environ["AGENT_MODEL"],
        os.getenv("AGENT_MODEL_BASE_URL", "https://api.openai.com/v1"),
        os.getenv("AGENT_MODEL_API_KEY"),
    )
