"""LlamaIndex: SDK tools as FunctionTools for any LlamaIndex agent or workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from llama_index.core.tools import FunctionTool

from agent_harness_sdk.adapters._common import pydantic_args, run_tool, selected
from agent_harness_sdk.approvals import Approver
from agent_harness_sdk.tools import Tool


def to_llamaindex_tools(
    tools: Iterable[Tool], *, include_mutating: bool = False, approver: Approver | None = None
) -> list[FunctionTool]:
    out = []
    for t in selected(list(tools), include_mutating):

        async def acall(_t=t, **kwargs):
            return await run_tool(_t, kwargs, framework="llamaindex", approver=approver)

        def call(_t=t, **kwargs):
            return asyncio.run(run_tool(_t, kwargs, framework="llamaindex", approver=approver))

        out.append(
            FunctionTool.from_defaults(
                fn=call, async_fn=acall, name=t.name, description=t.description, fn_schema=pydantic_args(t)
            )
        )
    return out
