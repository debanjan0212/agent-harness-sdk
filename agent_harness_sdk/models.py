"""Model providers. Every provider returns the same ModelReply, so the harness never cares which one runs.

DeepSeekModel        default. DeepSeek's OpenAI-compatible API (https://api.deepseek.com).
OpenAICompatibleModel OpenAI, Azure OpenAI v1, OpenRouter, Together, Groq, vLLM, Ollama, LiteLLM, ...
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
    """AGENT_MODEL_PROVIDER=deepseek (default) | openai. OpenAI-compatible endpoints read AGENT_MODEL_BASE_URL,
    AGENT_MODEL and AGENT_MODEL_API_KEY."""
    provider = os.getenv("AGENT_MODEL_PROVIDER", "deepseek")
    if provider == "deepseek":
        return DeepSeekModel()
    return OpenAICompatibleModel(
        os.environ["AGENT_MODEL"],
        os.getenv("AGENT_MODEL_BASE_URL", "https://api.openai.com/v1"),
        os.getenv("AGENT_MODEL_API_KEY"),
    )
