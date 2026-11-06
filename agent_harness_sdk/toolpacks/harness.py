"""Harness.io tool pack.

Two ways to give an agent Harness:

1. The official Harness MCP server (pipelines, executions, logs, and more), loaded through MCPToolset:

       async with MCPToolset(harness_mcp_server(), read_only={"list_pipelines", "get_execution", ...}) as h:
           tools = h.tools

2. These REST tools, which add what the agent needs to act: trigger a pipeline (e.g. a rollback pipeline).

Auth: HARNESS_API_KEY (a PAT or SAT, sent as x-api-key). The account id comes from HARNESS_ACCOUNT_ID or is read
from the PAT (pat.<account>.<id>.<secret>). Org/project: HARNESS_ORG_ID / HARNESS_PROJECT_ID.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agent_harness_sdk.tools import Tool, tool

DEFAULT_BASE = "https://app.harness.io"


class HarnessClient:
    def __init__(
        self,
        api_key: str | None = None,
        account: str | None = None,
        org: str | None = None,
        project: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key or os.environ.get("HARNESS_API_KEY", "")
        parts = self.api_key.split(".")
        self.account = account or os.getenv("HARNESS_ACCOUNT_ID") or (parts[1] if len(parts) >= 4 else "")
        # HARNESS_DEFAULT_* are the names Harness's own MCP server reads, so one .env serves both
        self.org = org or os.getenv("HARNESS_ORG_ID") or os.getenv("HARNESS_DEFAULT_ORG_ID", "default")
        self.project = project or os.getenv("HARNESS_PROJECT_ID") or os.getenv("HARNESS_DEFAULT_PROJECT_ID", "")
        self.base = (base_url or os.getenv("HARNESS_BASE_URL", DEFAULT_BASE)).rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=30,
            transport=transport,
            headers={"x-api-key": self.api_key, "Harness-Account": self.account, "Content-Type": "application/json"},
        )

    def _scope(self, **extra: Any) -> dict:
        return {
            "accountIdentifier": self.account,
            "orgIdentifier": self.org,
            "projectIdentifier": self.project,
            **extra,
        }

    async def _req(self, method: str, path: str, **kw: Any) -> dict:
        resp = await self.http.request(method, self.base + path, **kw)
        if resp.status_code >= 400:
            raise RuntimeError("Harness %s %s -> HTTP %s: %s" % (method, path, resp.status_code, resp.text[:300]))
        return resp.json()

    async def list_executions(self, pipeline: str, size: int = 5) -> list[dict]:
        data = await self._req(
            "POST",
            "/pipeline/api/pipelines/execution/summary",
            params=self._scope(pipelineIdentifier=pipeline, page=0, size=size),
            json={"filterType": "PipelineExecution"},
        )
        rows = (data.get("data") or {}).get("content") or []
        return [
            {
                "execution_id": r.get("planExecutionId"),
                "status": r.get("status"),
                "started_ms": r.get("startTs"),
                "ended_ms": r.get("endTs"),
                "trigger": (r.get("executionTriggerInfo") or {}).get("triggerType"),
            }
            for r in rows
        ]

    async def get_execution(self, execution_id: str) -> dict:
        data = await self._req("GET", "/pipeline/api/pipelines/execution/v2/" + execution_id, params=self._scope())
        s = (data.get("data") or {}).get("pipelineExecutionSummary") or {}
        return {
            "execution_id": execution_id,
            "pipeline": s.get("pipelineIdentifier"),
            "status": s.get("status"),
            "started_ms": s.get("startTs"),
            "ended_ms": s.get("endTs"),
            "failure": (s.get("failureInfo") or {}).get("message", ""),
        }

    async def execute(self, pipeline: str, inputs_yaml: str = "") -> dict:
        path = "/v1/orgs/%s/projects/%s/pipelines/%s/execute" % (self.org, self.project, pipeline)
        data = await self._req("POST", path, json={"inputs_yaml": inputs_yaml})
        execution = data.get("execution_details") or data.get("data") or data
        return {
            "pipeline": pipeline,
            "execution_id": execution.get("execution_id") or execution.get("planExecutionId"),
            "status": execution.get("status") or "submitted",
        }


def harness_tools(client: HarnessClient | None = None, *, rollback_pipeline: str | None = None) -> list[Tool]:
    """Read tools (executions) plus mutating tools (trigger a pipeline, roll a service back)."""
    c = client or HarnessClient()
    rollback_pipeline = rollback_pipeline or os.getenv("HARNESS_ROLLBACK_PIPELINE", "rollback")

    @tool(tags=("harness",))
    async def harness_recent_executions(pipeline: str, limit: int = 5) -> list:
        """Recent executions of a Harness pipeline (newest first) with status and trigger."""
        return await c.list_executions(pipeline, limit)

    @tool(tags=("harness",))
    async def harness_execution_status(execution_id: str) -> dict:
        """Status and failure message of one Harness pipeline execution."""
        return await c.get_execution(execution_id)

    @tool(mutating=True, tags=("harness",))
    async def harness_trigger_pipeline(pipeline: str, inputs_yaml: str = "") -> dict:
        """Trigger a Harness pipeline with optional runtime inputs YAML. Changes production: needs approval."""
        return await c.execute(pipeline, inputs_yaml)

    @tool(mutating=True, tags=("harness",))
    async def harness_rollback(service: str, to_version: str, environment: str = "prod") -> dict:
        """Roll a service back to a known-good version by running the rollback pipeline. Needs approval."""
        inputs = (
            "pipeline:\n  variables:\n    - name: service\n      value: %s\n    - name: version\n      value: %s\n"
            "    - name: environment\n      value: %s\n" % (service, to_version, environment)
        )
        return await c.execute(rollback_pipeline, inputs)

    return [harness_recent_executions, harness_execution_status, harness_trigger_pipeline, harness_rollback]


HARNESS_MCP_READ_ONLY = {
    "list_pipelines",
    "get_pipeline",
    "list_executions",
    "get_execution",
    "fetch_execution_url",
    "download_execution_logs",
}


def harness_mcp_server(image: str = "harness/mcp-server"):
    """StdioServerParameters that start Harness's official MCP server in Docker with your env credentials."""
    from mcp import StdioServerParameters

    env_names = ("HARNESS_API_KEY", "HARNESS_DEFAULT_ORG_ID", "HARNESS_DEFAULT_PROJECT_ID", "HARNESS_BASE_URL")
    args = ["run", "-i", "--rm"]
    for name in env_names:
        if os.getenv(name):
            args += ["-e", name]
    return StdioServerParameters(
        command="docker",
        args=[*args, image, "stdio"],
        env={k: os.environ[k] for k in env_names if os.getenv(k)} | {"PATH": os.getenv("PATH", "")},
    )
