"""agent-harness doctor | serve-mcp"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys


def doctor() -> int:
    """Show which model, telemetry and integrations are configured, and send one test span/log/metric."""
    from agent_harness_sdk import get_logger, setup_telemetry
    from agent_harness_sdk.telemetry import instruments

    tel = setup_telemetry("agent-harness-doctor", console="text", level="TRACE")
    log = get_logger("agent_harness_sdk.doctor")
    checks = {
        "model provider": os.getenv("AGENT_MODEL_PROVIDER", "deepseek"),
        "DEEPSEEK_API_KEY": "set" if os.getenv("DEEPSEEK_API_KEY") else "missing (ScriptedModel still works)",
        "DEEPSEEK_MODEL": os.getenv("DEEPSEEK_MODEL", "deepseek-flash (default)"),
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "not set (console only)"),
        "HARNESS_API_KEY": "set" if os.getenv("HARNESS_API_KEY") else "not set",
    }
    for extra in ("mcp", "langchain_core", "langgraph", "llama_index.core", "agents", "crewai", "fastapi"):
        try:
            found = importlib.util.find_spec(extra) is not None
        except ModuleNotFoundError:  # parent package missing, e.g. llama_index
            found = False
        checks["extra " + extra] = "installed" if found else "not installed"
    for k, v in checks.items():
        print("{:<32} {}".format(k, v))
    for level in ("trace", "debug", "info", "warn", "error", "fatal"):
        getattr(log, level)("doctor test log at %s", level.upper(), check="severity")
    log.event("agent.doctor", "doctor test event")
    instruments().runs.add(1, {"gen_ai.agent.name": "doctor", "agent.run.status": "test"})
    tel.force_flush()
    return 0


def serve_mcp(target: str) -> int:
    """Serve tools from module:attribute (a list of Tools or a ToolRegistry) over MCP stdio."""
    from agent_harness_sdk.mcp import serve_mcp as build

    module, _, attr = target.partition(":")
    tools = getattr(importlib.import_module(module), attr or "TOOLS")
    build(tools() if callable(tools) else tools).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check configuration and send test telemetry")
    s = sub.add_parser("serve-mcp", help="serve module:attr tools over MCP stdio")
    s.add_argument("target")
    a = p.parse_args(argv)
    return doctor() if a.cmd == "doctor" else serve_mcp(a.target)


if __name__ == "__main__":
    sys.exit(main())
