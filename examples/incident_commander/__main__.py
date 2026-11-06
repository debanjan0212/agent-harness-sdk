"""python -m incident_commander demo [--scenario bad-deploy|db-pool|injection] [--approve auto|ask|deny]
python -m incident_commander serve [--port 8080]"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from agent_harness_sdk import ApprovalResponse, AutoApprove, CallbackApprover, DenyAll, setup_telemetry


def ask(request) -> ApprovalResponse:
    print("\n  APPROVAL NEEDED  %s %s" % (request.action, request.arguments))
    print("  why: %s (risk %s, confidence %.2f)" % (request.rationale, request.risk, request.confidence))
    answer = input("  approve? [y/N] ").strip().lower()
    return ApprovalResponse(answer in ("y", "yes"), os.getenv("USER", "operator"))


def demo(scenario: str, approve: str) -> int:
    from .commander import IncidentCommander
    from .sources import FixtureSource

    approver = {"auto": AutoApprove(), "deny": DenyAll(), "ask": CallbackApprover(ask, "console")}[approve]
    source = FixtureSource(scenario)
    cmd = IncidentCommander(source=source, approver=approver)
    cmd.listeners.append(lambda iid, kind, detail: print("  %-10s %s" % (kind, detail)))
    print("ALERT  %s  %s" % (source.alert["labels"]["alertname"], source.alert["annotations"]["summary"]))
    inc = asyncio.run(cmd.handle(source.alert))
    print("\nINCIDENT %s  %s  %s\n" % (inc["id"], inc["severity"], inc["status"].upper()))
    print(inc["postmortem"])
    return 0


def serve(port: int) -> int:
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_config=None)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="incident_commander")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--scenario", default="bad-deploy")
    d.add_argument("--approve", default="ask" if sys.stdin.isatty() else "auto", choices=["auto", "ask", "deny"])
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    a = p.parse_args(argv)
    setup_telemetry("incident-commander", console=os.getenv("LOG_FORMAT", "text"), level=os.getenv("LOG_LEVEL", "WARN"))
    return demo(a.scenario, a.approve) if a.cmd == "demo" else serve(a.port)


if __name__ == "__main__":
    sys.exit(main())
