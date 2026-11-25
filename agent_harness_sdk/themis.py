"""Run your agents under Themis, the SRE control plane for agent fleets (github.com/debanjan0212/themis-agent-sre).

Themis watches the OpenTelemetry the harness already sends, gives every agent an error budget, and moves an
agent that burns it too fast from autonomous to supervised to suspended. This module makes the harness obey:

    gate = ThemisGate("http://themis:8600", os.environ["THEMIS_INGEST_KEY"]).start()
    agent = Agent(
        "deployer",
        model=...,
        tools=[...],
        policy=ThemisPolicy(gate=gate),      # plus any Policy settings you already use
        approver=ThemisApprover(gate),        # go / no-go is answered by a person in the Themis dashboard
    )

What each autonomy level means for a harness agent:

    autonomous   your Policy decides, as without Themis
    supervised   every action needs a person, even one your Policy would let run alone
    suspended    the agent may still read and analyse, but every action it proposes goes to a person instead

A tool outside the agent's allow-list in Themis is blocked and reported (five reports in five minutes suspend
the agent), and a tool Themis has quarantined is blocked until the quarantine ends. Each block is recorded as a
denied tool span, so Themis names the agent as the cause of the failed run.

The gate keeps a snapshot of Themis's decisions and refreshes it every second in a background thread, so a
check costs nothing and agents keep working on the last snapshot if Themis is briefly down. Approvals are the
exception: no answer from Themis is a no.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from agent_harness_sdk.approvals import ApprovalRequest, ApprovalResponse
from agent_harness_sdk.guardrails import Policy, PolicyVerdict, redact
from agent_harness_sdk.telemetry import INSTRUMENTATION, get_logger

log = get_logger(__name__)

AUTONOMOUS, SUPERVISED, SUSPENDED = "autonomous", "supervised", "suspended"
MAX_OUTBOX = 10_000  # reports waiting for Themis to come back; beyond this the oldest are dropped


class ThemisGate:
    """What an agent runtime needs from Themis, kept as a snapshot that refreshes itself."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        refresh_s: float = 1.0,
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url, self.refresh_s, self.timeout_s = url.rstrip("/"), refresh_s, timeout_s
        self._headers = {"Authorization": "Bearer " + key}
        self._transport = transport
        self._http = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout_s, transport=transport)
        self.snapshot: dict[str, Any] = {"agents": {}, "quarantine": {}, "fallbacks": {}, "faults": []}
        self.synced_at = 0.0
        self._outbox: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, **kw: Any) -> ThemisGate | None:
        """THEMIS_URL and THEMIS_INGEST_KEY, or None when they are not set."""
        url, key = os.getenv("THEMIS_URL", ""), os.getenv("THEMIS_INGEST_KEY", "")
        return cls(url, key, **kw) if url and key else None

    # ------------------------------------------------------------------ lifecycle
    def sync(self) -> None:
        """Pull the latest decisions and push the reports that are waiting. Raises httpx.HTTPError."""
        snapshot = self._http.get("/v1/gate/snapshot").raise_for_status().json()
        self.snapshot, self.synced_at = snapshot, time.time()
        with self._lock:
            batch, self._outbox = self._outbox[:500], self._outbox[500:]
        if batch:
            try:
                self._http.post("/v1/gate/events", json={"events": batch}).raise_for_status()
            except httpx.HTTPError:
                with self._lock:  # a lost report would under-count violations: keep it for next time
                    self._outbox = (batch + self._outbox)[-MAX_OUTBOX:]
                raise

    def start(self) -> ThemisGate:
        """Sync once now, then every refresh_s in a background thread. Returns the gate, for one-line setup."""
        self._try_sync()
        self._thread = threading.Thread(target=self._loop, name="themis-gate", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop refreshing and send any reports still waiting."""
        self._stop.set()
        if self._thread:
            self._thread.join(self.timeout_s + self.refresh_s)
        self._try_sync()
        self._http.close()

    def __enter__(self) -> ThemisGate:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.refresh_s):
            self._try_sync()

    def _try_sync(self) -> None:
        try:
            self.sync()
        except httpx.HTTPError as exc:  # Themis down: keep the last snapshot, agents keep working
            log.warning(
                "Themis at %s not reachable, keeping the snapshot from %.0fs ago: %s",
                self.url,
                time.time() - self.synced_at if self.synced_at else 0,
                exc,
            )

    # ------------------------------------------------------------------ questions a runtime asks before it acts
    def autonomy(self, agent: str) -> str:
        return self.snapshot["agents"].get(agent, {}).get("autonomy", AUTONOMOUS)

    def allowed(self, agent: str, kind: str, name: str) -> bool:
        """kind is "tools" or "handoffs". An agent with no allow-list in Themis is not restricted."""
        allow = self.snapshot["agents"].get(agent, {}).get(kind)
        return not allow or name in allow

    def high_risk(self, agent: str, tool: str) -> bool:
        return tool in self.snapshot["agents"].get(agent, {}).get("high_risk", ())

    def quarantined(self, component: str) -> bool:
        return self.snapshot["quarantine"].get(component, 0) > time.time()

    def fallback(self, model: str) -> str | None:
        return self.snapshot["fallbacks"].get(model)

    def report(self, agent: str, action: str, decision: str, reason: str = "") -> None:
        """decision is "deny" for an action outside the allow-list. Sent with the next sync."""
        event = {"agent": agent, "action": action, "decision": decision, "reason": reason[:300]}
        with self._lock:
            self._outbox = (self._outbox + [event])[-MAX_OUTBOX:]

    async def approve(
        self, agent: str, action: str, detail: dict, timeout_s: float = 300.0, poll_s: float = 1.0
    ) -> str:
        """Ask a person for go / no-go in Themis and wait. Returns approved, denied, expired or unreachable."""
        deadline = time.monotonic() + timeout_s
        async with httpx.AsyncClient(
            base_url=self.url, headers=self._headers, timeout=self.timeout_s, transport=self._async_transport()
        ) as http:
            try:
                created = await http.post("/v1/approvals", json={"agent": agent, "action": action, "detail": detail})
                approval_id = created.raise_for_status().json()["id"]
                while time.monotonic() < deadline:
                    status = (await http.get("/v1/approvals/" + approval_id)).raise_for_status().json()["status"]
                    if status != "pending":
                        return status
                    await asyncio.sleep(poll_s)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("approval for %s %s could not be asked in Themis: %s", agent, action, exc)
                return "unreachable"
        return "expired"

    def _async_transport(self) -> httpx.AsyncBaseTransport | None:
        t = self._transport
        return t if isinstance(t, httpx.AsyncBaseTransport) else None


def _denied_tool_span(action: str, decision: str, reason: str, error_type: str) -> None:
    """The attempted tool call, as a span that never ran, so Themis can blame the agent for the failed run."""
    attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": action,
        "themis.gate.decision": decision,
        "themis.gate.reason": reason[:300],
        "error.type": error_type,
    }
    with trace.get_tracer(INSTRUMENTATION).start_as_current_span(
        "execute_tool " + action, attributes=attributes
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason[:200]))


@dataclass
class ThemisPolicy(Policy):
    """Your Policy, plus what Themis has decided about this agent. Without a gate it is a plain Policy."""

    gate: ThemisGate | None = None

    def evaluate(self, decision, run) -> PolicyVerdict:
        verdict = super().evaluate(decision, run)
        if self.gate is None or decision.action in ("no_action", "escalate"):
            return verdict
        agent, action, gate = run.agent.name, decision.action, self.gate
        level = gate.autonomy(agent)
        blocks = []
        if level == SUSPENDED:
            blocks.append(("deny", "Themis has suspended %s, so a person must take this action" % agent))
        if not gate.allowed(agent, "tools", action):
            why = "%s is not in %s's allow-list in Themis" % (action, agent)
            gate.report(agent, "tool:" + action, "deny", why)  # only this one is a violation by the agent
            blocks.append(("deny", why))
        if gate.quarantined("tool:" + action):
            blocks.append(("deny", "Themis has quarantined tool:%s" % action))
        if blocks:
            reasons = [why for _, why in blocks]
            _denied_tool_span(action, "deny", "; ".join(reasons), "policy_denied")
            log.warning("Themis blocked %s for %s: %s", action, agent, "; ".join(reasons), run_id=run.id)
            return PolicyVerdict(False, False, verdict.reasons + reasons)
        if verdict.allowed and level == SUPERVISED and not verdict.requires_approval:
            return PolicyVerdict(True, True, verdict.reasons + ["Themis has %s under supervision" % agent])
        return verdict


class ThemisApprover:
    """Approver that puts the go / no-go in the Themis dashboard, next to the agent's error budget.

    No answer within timeout_s, or Themis out of reach, is a no. Themis expires an unanswered request after five
    minutes, so a longer timeout_s does not wait longer.
    """

    def __init__(self, gate: ThemisGate, timeout_s: float = 300.0, poll_s: float = 1.0):
        self.gate, self.timeout_s, self.poll_s = gate, timeout_s, poll_s

    async def review(self, request: ApprovalRequest) -> ApprovalResponse:
        arguments, _ = redact(json.dumps(request.arguments, default=str))
        detail = {
            "run_id": request.run_id,
            "arguments": arguments[:2000],
            "rationale": redact(request.rationale)[0][:1000],
            "risk": request.risk,
            "confidence": request.confidence,
            "summary": redact(request.summary)[0][:1000],
        }
        action = "tool:" + request.action
        status = await self.gate.approve(request.agent, action, detail, self.timeout_s, self.poll_s)
        if status == "approved":
            return ApprovalResponse(True, "themis", "approved in Themis")
        if status == "denied":
            _denied_tool_span(request.action, "denied_by_human", "rejected in Themis", "approval_denied")
            return ApprovalResponse(False, "themis", "rejected in Themis")
        why = {"expired": "nobody answered in Themis in time", "unreachable": "Themis could not be reached"}
        return ApprovalResponse(False, "themis", why.get(status, "Themis answered " + status))
