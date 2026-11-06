"""Where the commander's tools get their data.

FixtureSource  replays a recorded incident (demo, tests, video)
LiveSource     Prometheus + Loki + Harness through the SDK tool packs

Both expose the same async methods, so the tools in tools.py do not care which one is plugged in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_harness_sdk.toolpacks.harness import HarnessClient
from agent_harness_sdk.toolpacks.observability import summarize

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureSource:
    def __init__(self, scenario: str = "bad-deploy"):
        self.data = json.loads((FIXTURES / (scenario + ".json")).read_text())
        self.actions: list[dict] = []

    @property
    def alert(self) -> dict:
        return self.data["alert"]

    async def health(self, service: str) -> dict:
        return self.data["health"].get(service) or {"error": "no metrics for " + service}

    async def dependencies(self, service: str) -> list:
        return self.data["dependencies"].get(service, [])

    async def deploys(self, service: str) -> list:
        return self.data["deploys"].get(service, [])

    async def logs(self, service: str, minutes: int) -> dict:
        return summarize(self.data["logs"].get(service, []))

    async def rollback(self, service: str, to_version: str) -> dict:
        self.actions.append({"rollback": service, "to": to_version})
        return {
            "service": service,
            "rolled_back_to": to_version,
            "pipeline": "rollback",
            "execution_id": "exec-demo-1",
            "status": "Success",
        }

    async def scale(self, service: str, replicas: int) -> dict:
        self.actions.append({"scale": service, "replicas": replicas})
        return {"service": service, "replicas": replicas, "status": "scaled"}


class LiveSource:
    """Real backends. Metric/label names follow OpenTelemetry HTTP conventions; override with env vars."""

    def __init__(self, harness: HarnessClient | None = None):
        import httpx

        self.prom = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
        self.loki = os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/")
        self.metric = os.getenv("SLI_METRIC", "http_server_request_duration_seconds")
        self.label = os.getenv("SLI_SERVICE_LABEL", "service")
        self.deploy_pipeline = os.getenv("HARNESS_DEPLOY_PIPELINE", "deploy")
        self.harness = harness or HarnessClient()
        self.http = httpx.AsyncClient(timeout=20)

    async def _scalar(self, query: str):
        resp = await self.http.get(self.prom + "/api/v1/query", params={"query": query})
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        return round(float(result[0]["value"][1]), 4) if result else None

    async def health(self, service: str) -> dict:
        sel = '%s="%s"' % (self.label, service)
        total = "sum(rate(%s_count{%s}[5m]))" % (self.metric, sel)
        errors = 'sum(rate(%s_count{%s,http_response_status_code=~"5.."}[5m]))' % (self.metric, sel)
        p95 = "histogram_quantile(0.95, sum by (le) (rate(%s_bucket{%s}[5m]))) * 1000" % (self.metric, sel)
        return {
            "service": service,
            "rps": await self._scalar(total),
            "error_rate": await self._scalar("(%s / %s) or vector(0)" % (errors, total)),
            "error_rate_1h_ago": await self._scalar(
                "(%s / %s) or vector(0)"
                % (errors.replace("[5m]", "[5m] offset 1h"), total.replace("[5m]", "[5m] offset 1h"))
            ),
            "p95_ms": await self._scalar(p95),
        }

    async def dependencies(self, service: str) -> list:
        q = 'sum by (server) (rate(traces_service_graph_request_total{client="%s"}[15m]))' % service
        resp = await self.http.get(self.prom + "/api/v1/query", params={"query": q})
        resp.raise_for_status()
        return [r["metric"]["server"] for r in resp.json()["data"]["result"]]

    async def deploys(self, service: str) -> list:
        return await self.harness.list_executions(self.deploy_pipeline + "-" + service, 3)

    async def logs(self, service: str, minutes: int) -> dict:
        import time

        end = time.time_ns()
        resp = await self.http.get(
            self.loki + "/loki/api/v1/query_range",
            params={
                "query": '{service_name="%s"} |~ "(?i)error|exception|timeout"' % service,
                "start": end - minutes * 60 * 10**9,
                "end": end,
                "limit": 500,
            },
        )
        resp.raise_for_status()
        return summarize([v[1] for s in resp.json()["data"]["result"] for v in s["values"]])

    async def rollback(self, service: str, to_version: str) -> dict:
        inputs = (
            "pipeline:\n  variables:\n    - name: service\n      value: %s\n    - name: version\n      value: %s\n"
            % (service, to_version)
        )
        return await self.harness.execute(os.getenv("HARNESS_ROLLBACK_PIPELINE", "rollback"), inputs)

    async def scale(self, service: str, replicas: int) -> dict:
        inputs = (
            "pipeline:\n  variables:\n    - name: service\n      value: %s\n    - name: replicas\n      value: %d\n"
            % (service, replicas)
        )
        return await self.harness.execute(os.getenv("HARNESS_SCALE_PIPELINE", "scale"), inputs)
