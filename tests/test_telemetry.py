import json
import logging

from conftest import ROLLBACK, brain, get_errors, rollback
from opentelemetry import trace

from agent_harness_sdk import Agent, AutoApprove, get_logger
from agent_harness_sdk.telemetry import OTelJsonFormatter, severity

LEVELS = {
    "trace": (1, "TRACE"),
    "debug": (5, "DEBUG"),
    "info": (9, "INFO"),
    "warn": (13, "WARN"),
    "error": (17, "ERROR"),
    "fatal": (21, "FATAL"),
}


def logs_by_body(exporter):
    out = {}
    for item in exporter.get_finished_logs():
        rec = getattr(item, "log_record", item)
        out[rec.body] = rec
    return out


def test_every_severity_reaches_otel_with_the_right_number(telemetry):
    tel, _, logs, _ = telemetry
    log = get_logger("tests.severity")
    for level in LEVELS:
        getattr(log, level)("level %s works", level, check="severity")
    tel.force_flush()
    got = logs_by_body(logs)
    for level, (number, text) in LEVELS.items():
        rec = got["level %s works" % level]
        assert (int(rec.severity_number.value), rec.severity_text) == (number, text)
        assert rec.attributes["check"] == "severity" and rec.attributes["code.function.name"]


def test_logs_carry_trace_context_and_events_have_names(telemetry):
    tel, _, logs, _ = telemetry
    log = get_logger("tests.ctx")
    with trace.get_tracer("tests").start_as_current_span("outer") as span:
        log.event("agent.decision", "decided %s", "rollback", action="rollback")
        trace_id = span.get_span_context().trace_id
    tel.force_flush()
    rec = logs_by_body(logs)["decided rollback"]
    assert rec.event_name == "agent.decision"
    assert rec.trace_id == trace_id


def test_json_console_format_is_otel_shaped():
    rec = logging.LogRecord("svc", 5, __file__, 1, "raw %s", ("payload",), None)
    rec.otel_attributes = {"tool": "search_logs"}
    line = json.loads(OTelJsonFormatter(trace.get_tracer_provider().resource).format(rec))
    assert (line["severity_text"], line["severity_number"], line["body"]) == ("TRACE", 1, "raw payload")
    assert line["attributes"]["tool"] == "search_logs" and line["resource"]["service.name"] == "tests"
    assert severity(logging.CRITICAL)[1] == "FATAL" and severity(3)[1] == "TRACE"


def test_a_run_emits_genai_spans_and_metrics(telemetry):
    tel, spans, _, metrics = telemetry
    agent = Agent("melt-test", model=brain(ROLLBACK), tools=[get_errors, rollback], approver=AutoApprove())
    run = agent.run("checkout is failing")
    assert run.status == "completed"
    tel.force_flush()
    names = [s.name for s in spans.get_finished_spans()]
    assert "invoke_agent melt-test" in names and "execute_tool get_errors" in names and "chat scripted" in names
    assert {"agent.stage read", "agent.stage analyse", "agent.stage decide", "agent.stage act"} <= set(names)
    root = next(s for s in spans.get_finished_spans() if s.name == "invoke_agent melt-test")
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.conversation.id"] == run.id
    data = metrics.get_metrics_data()
    found = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {
        "gen_ai.client.token.usage",
        "gen_ai.client.operation.duration",
        "agent.runs",
        "agent.tool.calls",
        "agent.decisions",
        "agent.stage.duration",
        "gen_ai.invoke_agent.duration",
    } <= found
