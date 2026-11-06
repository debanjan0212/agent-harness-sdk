"""Automatic MELT (Metrics, Events, Logs, Traces) over OpenTelemetry.

One call wires all four signals:

    from agent_harness_sdk import setup_telemetry, get_logger
    setup_telemetry("incident-commander")          # OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set
    log = get_logger(__name__)
    log.trace("raw tool payload", tool="search_logs", bytes=812)
    log.info("deploy rolled back to %s", "1.3.2", service="checkout")

Logs use the OTel log data model with every severity: TRACE(1) DEBUG(5) INFO(9) WARN(13) ERROR(17) FATAL(21).
Python's own logging has no TRACE level and the stock OTel handler maps anything below DEBUG to UNSPECIFIED,
so this module adds TRACE and its own handler. Messages use lazy %-style arguments, never f-strings.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber, get_logger_provider, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

TRACE = 5
logging.addLevelName(TRACE, "TRACE")
INSTRUMENTATION = "agent_harness_sdk"

# Python level -> OTel severity (number, text). Checked top-down: first threshold the level reaches wins.
_SEVERITY = (
    (logging.CRITICAL, SeverityNumber.FATAL, "FATAL"),
    (logging.ERROR, SeverityNumber.ERROR, "ERROR"),
    (logging.WARNING, SeverityNumber.WARN, "WARN"),
    (logging.INFO, SeverityNumber.INFO, "INFO"),
    (logging.DEBUG, SeverityNumber.DEBUG, "DEBUG"),
)
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "otel_attributes",
    "otel_event_name",
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
}


def severity(levelno: int) -> tuple[SeverityNumber, str]:
    for threshold, number, text in _SEVERITY:
        if levelno >= threshold:
            return number, text
    return SeverityNumber.TRACE, "TRACE"


def otel_value(value: Any) -> Any:
    """Coerce to a type OTel attributes accept (str, bool, int, float or a list of one of them)."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
        return list(value)
    if isinstance(value, list | tuple) and all(isinstance(v, bool | int | float) for v in value):
        return list(value)
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def otel_attributes(attrs: dict | None) -> dict:
    return {str(k): otel_value(v) for k, v in (attrs or {}).items()}


def _record_attributes(record: logging.LogRecord) -> dict:
    attrs = {
        "code.function.name": record.funcName,
        "code.file.path": record.pathname,
        "code.line.number": record.lineno,
        "thread.name": record.threadName or "",
        "logger.name": record.name,
    }
    attrs.update({k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")})
    attrs.update(getattr(record, "otel_attributes", None) or {})
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        attrs["exception.type"] = type(exc).__name__
        attrs["exception.message"] = str(exc)
        attrs["exception.stacktrace"] = "".join(traceback.format_exception(*record.exc_info))
    return otel_attributes(attrs)


class OTelLogHandler(logging.Handler):
    """Sends Python log records to the OTel Logs SDK with correct severity for every level, TRACE included."""

    def __init__(self, provider: LoggerProvider, level: int = TRACE):
        super().__init__(level)
        self.provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        try:
            number, text = severity(record.levelno)
            self.provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                severity_number=number,
                severity_text=text,
                body=record.getMessage(),
                attributes=_record_attributes(record),
                event_name=getattr(record, "otel_event_name", None),
            )
        except Exception:  # noqa: BLE001 - logging must never crash the app
            self.handleError(record)


class OTelJsonFormatter(logging.Formatter):
    """One JSON line per record in the OTel log data model shape, for stdout / container log collectors."""

    def __init__(self, resource: Resource):
        super().__init__()
        self.resource = {k: otel_value(v) for k, v in resource.attributes.items()}

    def format(self, record: logging.LogRecord) -> str:
        number, text = severity(record.levelno)
        ctx = trace.get_current_span().get_span_context()
        line = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="microseconds"),
            "severity_text": text,
            "severity_number": number.value,
            "body": record.getMessage(),
            "attributes": _record_attributes(record),
            "resource": self.resource,
        }
        event = getattr(record, "otel_event_name", None)
        if event:
            line["event_name"] = event
        if ctx.is_valid:
            line["trace_id"] = format(ctx.trace_id, "032x")
            line["span_id"] = format(ctx.span_id, "016x")
        return json.dumps(line, default=str)


class OTelTextFormatter(logging.Formatter):
    """Readable single-line output for terminals: time, severity, logger, message, key=value attributes."""

    def format(self, record: logging.LogRecord) -> str:
        _, text = severity(record.levelno)
        extra = getattr(record, "otel_attributes", None) or {}
        tail = " ".join("{}={}".format(k, v) for k, v in extra.items())
        event = getattr(record, "otel_event_name", None)
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        message = record.getMessage()
        if event and message == event:
            message = ""
        parts = [stamp, text.ljust(5), record.name, ("[" + event + "]") if event else "", message, tail]
        out = " ".join(p for p in parts if p)
        if record.exc_info:
            out = out + "\n" + "".join(traceback.format_exception(*record.exc_info))
        return out


class AgentLogger:
    """Thin logger with all OTel severities and structured attributes. Use %-style args, never f-strings:

    log.info("rolled back %s to %s", service, version, incident_id="INC-42")
    """

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def _emit(self, level: int, msg: str, args: tuple, attrs: dict, exc_info=None, event: str | None = None) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(
                level,
                msg,
                *args,
                exc_info=exc_info,
                stacklevel=3,
                extra={"otel_attributes": attrs, "otel_event_name": event},
            )

    def trace(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(TRACE, msg, args, attrs)

    def debug(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.DEBUG, msg, args, attrs)

    def info(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.INFO, msg, args, attrs)

    def warn(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.WARNING, msg, args, attrs)

    warning = warn

    def error(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.ERROR, msg, args, attrs)

    def exception(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.ERROR, msg, args, attrs, exc_info=True)

    def fatal(self, msg: str, *args: Any, **attrs: Any) -> None:
        self._emit(logging.CRITICAL, msg, args, attrs)

    critical = fatal

    def event(self, name: str, msg: str = "", *args: Any, level: int = logging.INFO, **attrs: Any) -> None:
        """An OTel event: a log record with event_name set (e.g. agent.decision)."""
        self._emit(level, msg or name, args, attrs, event=name)

    def is_enabled(self, level: int) -> bool:
        return self._log.isEnabledFor(level)


def get_logger(name: str) -> AgentLogger:
    return AgentLogger(logging.getLogger(name))


@dataclass
class Instruments:
    """Metric instruments. GenAI ones follow the OTel GenAI semantic conventions (development status)."""

    token_usage: Any
    operation_duration: Any
    tool_duration: Any
    agent_duration: Any
    runs: Any
    tool_calls: Any
    decisions: Any
    guardrail_events: Any
    stage_duration: Any


def instruments() -> Instruments:
    m = metrics.get_meter(INSTRUMENTATION)
    return Instruments(
        token_usage=m.create_histogram(
            "gen_ai.client.token.usage", unit="{token}", description="Input and output tokens per model call"
        ),
        operation_duration=m.create_histogram(
            "gen_ai.client.operation.duration", unit="s", description="Model call duration"
        ),
        tool_duration=m.create_histogram("gen_ai.execute_tool.duration", unit="s", description="Tool call duration"),
        agent_duration=m.create_histogram("gen_ai.invoke_agent.duration", unit="s", description="Agent run duration"),
        runs=m.create_counter("agent.runs", unit="{run}", description="Agent runs by outcome"),
        tool_calls=m.create_counter("agent.tool.calls", unit="{call}", description="Tool calls by tool and outcome"),
        decisions=m.create_counter("agent.decisions", unit="{decision}", description="Decisions by action and verdict"),
        guardrail_events=m.create_counter(
            "agent.guardrail.events", unit="{event}", description="Guardrail blocks, redactions and injection flags"
        ),
        stage_duration=m.create_histogram("agent.stage.duration", unit="s", description="Duration per stage"),
    )


@dataclass
class Telemetry:
    resource: Resource
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider

    def shutdown(self) -> None:
        for p in (self.tracer_provider, self.meter_provider, self.logger_provider):
            try:
                p.shutdown()
            except Exception:  # noqa: BLE001 - best effort at exit
                logging.getLogger(INSTRUMENTATION).debug("telemetry shutdown failed", exc_info=True)

    def force_flush(self) -> None:
        self.tracer_provider.force_flush()
        self.meter_provider.force_flush()
        self.logger_provider.force_flush()


_LOCK = threading.Lock()
_ACTIVE: Telemetry | None = None


def setup_telemetry(
    service_name: str,
    *,
    service_version: str = "0.1.0",
    environment: str | None = None,
    otlp_endpoint: str | None = None,
    console: str | None = "text",
    level: int | str = logging.INFO,
    resource_attributes: dict | None = None,
    span_exporter=None,
    metric_reader=None,
    log_exporter=None,
) -> Telemetry:
    """Wire traces, metrics and logs once per process.

    otlp_endpoint: OTLP/HTTP base URL (default: OTEL_EXPORTER_OTLP_ENDPOINT). Unset = no OTLP export.
    console:       "text" (readable), "json" (OTel log data model, one line per record) or None.
    level:         lowest log level: "TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL" or a number.
    span_exporter / metric_reader / log_exporter: inject your own (tests use the in-memory ones).
    """
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None:
            return _ACTIVE
        attrs = {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment.name": environment or os.getenv("DEPLOYMENT_ENVIRONMENT", "dev"),
            "telemetry.sdk.wrapper": "agent-harness-sdk",
        }
        attrs.update(resource_attributes or {})
        resource = Resource.create(attrs)
        endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

        tracer_provider = TracerProvider(resource=resource)
        readers = [metric_reader] if metric_reader else []
        logger_provider = LoggerProvider(resource=resource)
        if span_exporter:
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        if log_exporter:
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            base = endpoint.rstrip("/")
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces")))
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=base + "/v1/metrics"), export_interval_millis=15_000
                )
            )
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter(endpoint=base + "/v1/logs"))
            )
        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)

        root = logging.getLogger()
        root.setLevel(_level(level))
        root.addHandler(OTelLogHandler(logger_provider, level=TRACE))
        if console:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(OTelJsonFormatter(resource) if console == "json" else OTelTextFormatter())
            root.addHandler(stream)
        for noisy in ("httpx", "httpcore", "urllib3", "opentelemetry", "asyncio", "mcp"):
            logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
        _ACTIVE = Telemetry(resource, tracer_provider, meter_provider, logger_provider)
        get_logger(INSTRUMENTATION).debug("telemetry ready", otlp_endpoint=endpoint or "none", console=console or "off")
        return _ACTIVE


def _level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    named = {
        "TRACE": TRACE,
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "FATAL": logging.CRITICAL,
        "CRITICAL": logging.CRITICAL,
    }
    return named[level.upper()]


def reset_telemetry_for_tests() -> None:
    """Test helper: allow setup_telemetry to run again in the same process."""
    global _ACTIVE
    with _LOCK:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, OTelLogHandler) or isinstance(
                getattr(h, "formatter", None), OTelJsonFormatter | OTelTextFormatter
            ):
                root.removeHandler(h)
        _ACTIVE = None


def current_logger_provider():
    return get_logger_provider()
