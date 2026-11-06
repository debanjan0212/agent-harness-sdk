"""CrewAI pins mcp<2 and openai<3, so CI runs this file in its own environment with the crewai extra."""

import pytest

crewai = pytest.importorskip("crewai.tools", reason="crewai extra not installed in this environment")

from conftest import get_errors, rollback  # noqa: E402

from agent_harness_sdk.adapters.crewai import to_crewai_tools  # noqa: E402


def test_crewai_tools():
    tools = to_crewai_tools([get_errors, rollback])
    assert [t.name for t in tools] == ["get_errors"]
    assert "0.33" in tools[0].run(service="checkout")
