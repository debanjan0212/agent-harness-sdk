import sys
from pathlib import Path

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_harness_sdk import ModelReply, ScriptedModel, ToolCall, setup_telemetry, tool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))

SPANS, LOGS, METRICS = InMemorySpanExporter(), InMemoryLogRecordExporter(), InMemoryMetricReader()
TEL = setup_telemetry(
    "tests", console=None, level="TRACE", span_exporter=SPANS, metric_reader=METRICS, log_exporter=LOGS
)


@pytest.fixture
def telemetry():
    TEL.force_flush()
    SPANS.clear()
    LOGS.clear()
    return TEL, SPANS, LOGS, METRICS


@tool()
def get_errors(service: str) -> dict:
    """Error rate for a service."""
    return {"service": service, "error_rate": 0.33, "log": "api_key=sk_live_abc123 failed"}


@tool(mutating=True)
def rollback(service: str, version: str) -> dict:
    """Roll a service back."""
    return {"rolled_back": service, "to": version}


@tool(mutating=True, requires_approval=False)
def page(reason: str, run_id: str = "") -> dict:
    """Page a human."""
    return {"paged": True}


def brain(decision: dict, analysis: dict | None = None, read_calls=(("get_errors", {"service": "checkout"}),)):
    """A scripted model that reads, analyses, then returns `decision`."""

    def fn(messages, tools, json_mode):
        system = messages[0]["content"]
        if "Stage READ" in system:
            done = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
            todo = [ToolCall("c%d" % i, n, a) for i, (n, a) in enumerate(read_calls) if "c%d" % i not in done]
            return ModelReply(None, todo) if todo else "evidence collected"
        if "Stage ANALYSE" in system:
            return analysis or {
                "summary": "checkout failing",
                "findings": ["33% errors"],
                "likely_cause": "bad deploy",
                "confidence": 0.9,
            }
        return decision

    return ScriptedModel(fn)


ROLLBACK = {
    "action": "rollback",
    "arguments": {"service": "checkout", "version": "1.3.2"},
    "rationale": "bad deploy",
    "confidence": 0.9,
    "risk": "medium",
}
