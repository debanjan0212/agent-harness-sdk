"""The commander's tools. Read tools gather evidence; mutating tools change production and need approval."""

from __future__ import annotations

from agent_harness_sdk import Tool, tool
from agent_harness_sdk.toolpacks.observability import notify_tools


def build_tools(source, store, memory) -> list[Tool]:
    @tool(tags=("read",))
    async def service_health(service: str) -> dict:
        """Requests per second, error rate (now and 1 hour ago) and p95 latency for a service."""
        return await source.health(service)

    @tool(tags=("read",))
    async def service_dependencies(service: str) -> list:
        """Services this service calls (from the trace service graph)."""
        return await source.dependencies(service)

    @tool(tags=("read",))
    async def recent_deploys(service: str) -> list:
        """Recent deploys of a service: version, previous version, minutes ago, pipeline execution."""
        return await source.deploys(service)

    @tool(tags=("read",))
    async def search_logs(service: str, minutes: int = 30) -> dict:
        """Error log patterns for a service over the last N minutes, grouped with counts."""
        return await source.logs(service, minutes)

    @tool(tags=("read",))
    def similar_incidents(query: str) -> list:
        """Past incidents similar to this one, with what was done and how it ended."""
        return [{"summary": r.summary, "action": r.action, "outcome": r.outcome} for r in memory.recall(query, 3)]

    @tool(mutating=True, tags=("act",))
    async def rollback_deploy(service: str, to_version: str) -> dict:
        """Roll a service back to a previous version through the deploy pipeline. Needs approval."""
        return await source.rollback(service, to_version)

    @tool(mutating=True, tags=("act",))
    async def scale_service(service: str, replicas: int) -> dict:
        """Change the replica count of a service. Needs approval."""
        return await source.scale(service, replicas)

    @tool(mutating=True, requires_approval=False, tags=("act",))
    def page_oncall(reason: str, run_id: str = "") -> dict:
        """Page the human on-call engineer with the reason. Used for every escalation."""
        store.add(run_id, "paged", reason)
        return {"paged": True, "reason": reason}

    return [
        service_health,
        service_dependencies,
        recent_deploys,
        search_logs,
        similar_incidents,
        rollback_deploy,
        scale_service,
        page_oncall,
        *notify_tools(),
    ]
