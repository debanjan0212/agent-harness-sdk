"""OpenAI Agents SDK: SDK tools as FunctionTools (works with any model the Agents SDK supports)."""

from __future__ import annotations

import json
from collections.abc import Iterable

from agents import FunctionTool

from agent_harness_sdk.adapters._common import run_tool, selected
from agent_harness_sdk.approvals import Approver
from agent_harness_sdk.tools import Tool


def to_openai_agents_tools(
    tools: Iterable[Tool], *, include_mutating: bool = False, approver: Approver | None = None
) -> list[FunctionTool]:
    out = []
    for t in selected(list(tools), include_mutating):

        async def on_invoke(_ctx, args_json: str, _t=t) -> str:
            return await run_tool(_t, json.loads(args_json or "{}"), framework="openai-agents", approver=approver)

        out.append(
            FunctionTool(
                name=t.name,
                description=t.description,
                params_json_schema=t.parameters,
                on_invoke_tool=on_invoke,
                strict_json_schema=False,
            )
        )
    return out
