"""Tools: plain Python functions (sync or async) with typed arguments, declared read-only or mutating.

    @tool()
    async def query_metrics(service: str, minutes: int = 30) -> dict:
        '''Error rate and p95 latency for a service.'''

    @tool(mutating=True)
    def rollback(service: str, version: str) -> dict:
        '''Roll a service back to a version.'''

Read-only tools are the only ones the model sees while it gathers evidence. Mutating tools can only run
in the ACT stage, after the policy check, and (by default) after a human approves.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model


class ToolError(Exception):
    """Raised for bad arguments, timeouts or failures inside a tool."""


def _args_model(fn: Callable, name: str) -> type[BaseModel]:
    fields = {}
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        annotation = p.annotation if p.annotation is not inspect.Parameter.empty else Any
        default = p.default if p.default is not inspect.Parameter.empty else ...
        fields[pname] = (annotation, default)
    model_name = "".join(part.capitalize() for part in name.split("_")) + "Args"
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any] | None
    mutating: bool = False
    requires_approval: bool | None = None  # default: same as mutating
    timeout_s: float = 30.0
    tags: tuple[str, ...] = ()
    args_model: type[BaseModel] | None = None
    json_schema: dict | None = None  # for tools defined by schema only (e.g. imported over MCP)
    invoker: Callable[[dict], Any] | None = None  # custom transport (MCP, HTTP...) instead of calling fn
    source: str = "python"
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.requires_approval is None:
            self.requires_approval = self.mutating
        if self.args_model is None and self.json_schema is None and self.fn is not None:
            self.args_model = _args_model(self.fn, self.name)

    @property
    def parameters(self) -> dict:
        if self.json_schema is not None:
            return self.json_schema
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def validate(self, arguments: dict) -> dict:
        if self.args_model is None:
            required = set(self.parameters.get("required", []))
            missing = required - set(arguments)
            if missing:
                raise ToolError("missing required arguments: %s" % ", ".join(sorted(missing)))
            return dict(arguments)
        try:
            return self.args_model.model_validate(arguments).model_dump()
        except ValidationError as exc:
            raise ToolError("invalid arguments for %s: %s" % (self.name, exc.errors(include_url=False))) from exc

    async def invoke(self, arguments: dict) -> Any:
        args = self.validate(arguments)
        try:
            if self.invoker is not None:
                result = self.invoker(args)
            elif inspect.iscoroutinefunction(self.fn):
                result = self.fn(**args)
            else:
                result = asyncio.to_thread(self.fn, **args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=self.timeout_s)
            return result
        except TimeoutError as exc:
            raise ToolError("%s timed out after %ss" % (self.name, self.timeout_s)) from exc


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    mutating: bool = False,
    requires_approval: bool | None = None,
    timeout_s: float = 30.0,
    tags: Iterable[str] = (),
) -> Callable:
    """Decorator that turns a function into a Tool. The docstring becomes the description the model reads."""

    def wrap(fn: Callable) -> Tool:
        doc = inspect.getdoc(fn) or ""
        return Tool(
            name=name or fn.__name__,
            description=description or doc.strip() or fn.__name__,
            fn=fn,
            mutating=mutating,
            requires_approval=requires_approval,
            timeout_s=timeout_s,
            tags=tuple(tags),
        )

    return wrap


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, t: Tool) -> Tool:
        if not isinstance(t, Tool):
            raise TypeError("expected a Tool (decorate the function with @tool)")
        if t.name in self._tools:
            raise ValueError("duplicate tool name: %s" % t.name)
        self._tools[t.name] = t
        return t

    def extend(self, tools: Iterable[Tool]) -> ToolRegistry:
        for t in tools:
            self.add(t)
        return self

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def read_only(self) -> list[Tool]:
        return [t for t in self._tools.values() if not t.mutating]

    def mutating(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.mutating]
