"""SDK tools in a CrewAI crew. Install in its own env: pip install "agent-harness-sdk[crewai]"."""

from crewai import Agent, Crew, Task

from agent_harness_sdk import setup_telemetry, tool
from agent_harness_sdk.adapters.crewai import to_crewai_tools

setup_telemetry("crewai-example")


@tool()
def error_budget(service: str) -> dict:
    """Error budget left this month for a service."""
    return {"service": service, "budget_left_pct": 38}


sre = Agent(role="SRE", goal="Report error budget risk", backstory="On-call SRE", tools=to_crewai_tools([error_budget]))
crew = Crew(
    agents=[sre],
    tasks=[Task(description="How much budget does checkout have left?", expected_output="one line", agent=sre)],
)
print(crew.kickoff())  # needs an LLM configured for CrewAI (e.g. DEEPSEEK_API_KEY with model="deepseek/deepseek-flash")
