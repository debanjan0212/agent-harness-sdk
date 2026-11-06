"""IncidentCommander: a product built on agent-harness-sdk.

What the SDK gives:   the read/analyse/decide/act loop, tools, policy, approvals, memory, MELT telemetry.
What this wrapper adds: incident domain (severity, impact), a playbook prompt, domain tools, a domain rule,
                        an incident store with a timeline, a postmortem, and product metrics (MTTM).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from opentelemetry import metrics, trace

from agent_harness_sdk import (
    Agent,
    AgentEvent,
    Approver,
    DeepSeekModel,
    DenyAll,
    InMemoryMemory,
    Memory,
    Policy,
    ScriptedModel,
    get_logger,
)

from .brain import OfflineBrain
from .domain import PLAYBOOK, IncidentAnalysis
from .sources import FixtureSource, LiveSource
from .store import IncidentStore
from .tools import build_tools

log = get_logger("incident_commander")
meter = metrics.get_meter("incident_commander")
OPENED = meter.create_counter("incidents.opened", unit="{incident}", description="Incidents opened by severity")
MITIGATED = meter.create_counter("incidents.closed", unit="{incident}", description="Incidents by final status")
TTM = meter.create_histogram("incident.time_to_mitigate", unit="s", description="Alert to mitigation")


def rollback_needs_recent_deploy(decision, run) -> str | None:
    """Domain rule in code: only roll back a service that actually deployed in the last 2 hours."""
    if decision.action != "rollback_deploy":
        return None
    service = decision.arguments.get("service")
    for o in run.observations:
        if o.tool == "recent_deploys" and o.arguments.get("service") == service and '"minutes_ago"' in o.output:
            return None
    return "no recent deploy of %s in the evidence, so a rollback is not justified" % service


class IncidentCommander:
    def __init__(
        self,
        *,
        model=None,
        source=None,
        store: IncidentStore | None = None,
        memory: Memory | None = None,
        approver: Approver | None = None,
        policy: Policy | None = None,
    ):
        if source is None:  # IC_SOURCE=live reads Prometheus/Loki/Harness; anything else replays a fixture
            source = (
                LiveSource()
                if os.getenv("IC_SOURCE") == "live"
                else FixtureSource(os.getenv("IC_SCENARIO", "bad-deploy"))
            )
        self.source = source
        self.store = store or IncidentStore(os.getenv("IC_DB", ":memory:"))
        self.memory = memory or InMemoryMemory()
        if model is None:
            model = (
                DeepSeekModel() if os.getenv("DEEPSEEK_API_KEY") else ScriptedModel(OfflineBrain(), "offline-sre-brain")
            )
        self.agent = Agent(
            "incident-commander",
            model=model,
            tools=build_tools(self.source, self.store, self.memory),
            instructions=PLAYBOOK,
            analysis_schema=IncidentAnalysis,
            memory=self.memory,
            policy=policy or Policy(min_confidence=0.7, rules=[rollback_needs_recent_deploy]),
            approver=approver or DenyAll(),
            escalation_tool="page_oncall",
            hooks=[self._timeline],
        )
        self.listeners = []

    async def handle(self, alert: dict) -> dict:
        labels = alert.get("labels", {})
        service = labels.get("service") or labels.get("app") or "unknown"
        title = "%s on %s" % (labels.get("alertname", "Alert"), service)
        summary = (alert.get("annotations") or {}).get("summary", "")
        iid = self.store.open(title, service)
        t0 = time.time()
        with trace.get_tracer("incident_commander").start_as_current_span(
            "incident.handle",
            attributes={"incident.id": iid, "incident.service": service, "alert.name": labels.get("alertname", "")},
        ) as span:
            log.info("incident %s opened: %s", iid, title, incident_id=iid, service=service)
            run = await self.agent.arun("%s. %s" % (title, summary), context=alert, run_id=iid)
            a = run.analysis.model_dump() if run.analysis else {}
            out = run.outcome
            status = {"done": "mitigated", "no_action": "resolved"}.get(out.status if out else "", "escalated")
            if run.status in ("failed", "timeout"):
                status = "escalated"
            self.store.update(
                iid,
                severity=a.get("severity", "SEV?"),
                status=status,
                summary=a.get("summary", ""),
                cause=a.get("likely_cause", ""),
                action=out.action if out else "",
                action_status=out.status if out else run.status,
                analysis=a,
                mitigated_at=time.time() if status == "mitigated" else None,
            )
            span.set_attribute("incident.severity", a.get("severity", "SEV?"))
            span.set_attribute("incident.status", status)
        OPENED.add(1, {"incident.severity": a.get("severity", "SEV?")})
        MITIGATED.add(1, {"incident.status": status})
        if status == "mitigated":
            TTM.record(time.time() - t0, {"incident.service": service})
        self.store.add(iid, "status", status)
        self.store.update(iid, postmortem=self.postmortem(iid))
        log.info("incident %s %s (%s)", iid, status, a.get("severity", "SEV?"), incident_id=iid)
        return self.store.get(iid)

    def _timeline(self, e: AgentEvent) -> None:
        d = e.data
        text = {
            "agent.stage.start": lambda: ("stage", e.stage.upper()),
            "agent.observation": lambda: (
                "evidence",
                "%s(%s)%s"
                % (
                    d.get("tool"),
                    ", ".join("%s=%s" % kv for kv in (d.get("arguments") or {}).items()),
                    "" if d.get("ok") else " FAILED",
                ),
            ),
            "agent.analysis": lambda: ("analysis", "%s: %s" % (d.get("severity", ""), d.get("summary", ""))),
            "agent.decision": lambda: (
                "decision",
                "%s %s%s"
                % (
                    d["final"]["action"],
                    d["final"]["arguments"] or "",
                    "" if d.get("allowed") else " (policy blocked " + d["proposed"]["action"] + ")",
                ),
            ),
            "agent.approval.requested": lambda: ("approval", "requested for " + d.get("action", "")),
            "agent.approval": lambda: (
                "approval",
                ("approved" if d.get("approved") else "denied") + " by " + d.get("by", ""),
            ),
            "agent.action": lambda: ("action", "%s -> %s" % (d.get("action"), d.get("status"))),
            "agent.guardrail": lambda: ("guardrail", str(d)),
        }.get(e.type)
        if text:
            kind, detail = text()
            self.store.add(e.run_id, kind, detail)
            for fn in self.listeners:
                fn(e.run_id, kind, detail)

    def postmortem(self, iid: str) -> str:
        inc = self.store.get(iid)
        a = inc.get("analysis") or {}
        t0 = inc["opened_at"]
        rows = [
            "| +%5.1fs | %s | %s |" % (e["ts"] - t0, e["kind"], e["detail"].replace("|", "/")) for e in inc["timeline"]
        ]
        opened = datetime.fromtimestamp(t0, UTC).strftime("%Y-%m-%d %H:%M UTC")
        follow = [
            "Add a check to the release pipeline that would have caught: " + (a.get("likely_cause") or "the cause"),
            "Confirm the canary/SLO gate for %s fails fast on this error pattern" % inc["service"],
            "Review the alert: it fired on the symptom; consider alerting on the cause too",
        ]
        lines = [
            "# Postmortem draft: %s" % inc["title"],
            "",
            "**Incident:** %s · **Severity:** %s · **Status:** %s · **Opened:** %s"
            % (iid, inc["severity"], inc["status"], opened),
            "",
            "## Summary",
            a.get("summary", ""),
            "",
            "## Customer impact",
            a.get("customer_impact", "") or "unknown",
            "",
            "## Root cause",
            a.get("likely_cause", "") or "not determined automatically",
            "",
            "## Evidence",
            *["- " + f for f in a.get("findings", [])],
            "",
            "## Action taken",
            "%s (%s)" % (inc.get("action") or "none", inc.get("action_status") or "-"),
            "",
            "## Timeline",
            "| time | step | detail |",
            "|---|---|---|",
            *rows,
            "",
            "## Follow-ups",
            *["- [ ] " + f for f in follow],
            "",
            "_Drafted by Incident Commander (agent-harness-sdk). Review before publishing._",
        ]
        return "\n".join(lines)
