"""CrewAI: SDK tools as CrewAI BaseTools.

Note: crewai currently pins mcp<2 and openai<3, which conflicts with the `mcp` and `openai-agents` extras.
Install the `crewai` extra in its own environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from crewai.tools import BaseTool

from agent_harness_sdk.adapters._common import pydantic_args, run_tool, selected
from agent_harness_sdk.approvals import Approver
from agent_harness_sdk.tools import Tool


def to_crewai_tools(
    tools: Iterable[Tool], *, include_mutating: bool = False, approver: Approver | None = None
) -> list[BaseTool]:
    out = []
    for t in selected(list(tools), include_mutating):

        def _run(self, _t=t, **kwargs):
            return asyncio.run(run_tool(_t, kwargs, framework="crewai", approver=approver))

        async def _arun(self, _t=t, **kwargs):
            return await run_tool(_t, kwargs, framework="crewai", approver=approver)

        cls_name = t.name.title().replace("_", "") + "Tool"
        cls = type(
            cls_name,
            (BaseTool,),
            {
                "__module__": __name__,
                "__qualname__": cls_name,
                "__annotations__": {"name": str, "description": str, "args_schema": type},
                "name": t.name,
                "description": t.description,
                "args_schema": pydantic_args(t),
                "_run": _run,
                "_arun": _arun,
            },
        )
        out.append(cls())
    return out
