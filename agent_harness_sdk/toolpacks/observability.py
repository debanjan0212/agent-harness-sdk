"""Observability tool pack: Prometheus (PromQL), Loki (LogQL) and a Slack/Google Chat notifier.

These speak open APIs, so they work against self-hosted stacks or any service that exposes the same APIs.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter

import httpx

from agent_harness_sdk.tools import Tool, tool

_NUM = re.compile(r"\d+(\.\d+)?")
_HEX = re.compile(r"\b(0x)?[0-9a-f]{8,}\b", re.I)


def prometheus_tools(url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> list[Tool]:
    base = (url or os.getenv("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
    http = httpx.AsyncClient(timeout=20, transport=transport)

    @tool(tags=("metrics",))
    async def promql(query: str) -> list:
        """Run a PromQL instant query. Returns up to 20 series as {labels, value}."""
        resp = await http.get(base + "/api/v1/query", params={"query": query})
        resp.raise_for_status()
        return [{"labels": r["metric"], "value": r["value"][1]} for r in resp.json()["data"]["result"][:20]]

    return [promql]


def summarize(lines: list[str], limit: int = 5) -> dict:
    """Group log lines into patterns (numbers and ids masked) so the model sees signal, not noise."""
    patterns = Counter(_NUM.sub("<n>", _HEX.sub("<id>", line)) for line in lines)
    return {
        "lines": len(lines),
        "top_patterns": [{"pattern": p, "count": n} for p, n in patterns.most_common(limit)],
        "samples": lines[:limit],
    }


def loki_tools(url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> list[Tool]:
    base = (url or os.getenv("LOKI_URL", "http://localhost:3100")).rstrip("/")
    http = httpx.AsyncClient(timeout=20, transport=transport)

    @tool(tags=("logs",))
    async def logql(query: str, minutes: int = 30, limit: int = 500) -> dict:
        """Run a LogQL query over the last N minutes. Returns grouped error patterns with counts and samples."""
        end = time.time_ns()
        resp = await http.get(
            base + "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": end - minutes * 60 * 10**9,
                "end": end,
                "limit": limit,
                "direction": "backward",
            },
        )
        resp.raise_for_status()
        lines = [v[1] for s in resp.json()["data"]["result"] for v in s["values"]]
        return summarize(lines)

    return [logql]


def notify_tools(webhook_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> list[Tool]:
    hook = webhook_url or os.getenv("NOTIFY_WEBHOOK_URL", "")
    http = httpx.AsyncClient(timeout=10, transport=transport)

    # Posting a message changes nothing in production, so it needs no approval.
    @tool(mutating=True, requires_approval=False, tags=("notify",))
    async def notify(text: str) -> dict:
        """Post a message to the team channel (Slack or Google Chat incoming webhook)."""
        if not hook:
            return {"posted": False, "reason": "NOTIFY_WEBHOOK_URL is not set", "text": text}
        resp = await http.post(hook, json={"text": text})
        return {"posted": resp.status_code < 300, "status": resp.status_code}

    return [notify]
