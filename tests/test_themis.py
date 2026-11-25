"""The harness under Themis. A small fake of Themis's gate API (the same routes and answers as the real one) stands
in here; themis-agent-sre's ecosystem tests run these classes against the real control plane."""

import json
import time

import httpx
import pytest
from conftest import ROLLBACK, brain, get_errors, page, rollback

from agent_harness_sdk import Agent, AutoApprove, ThemisApprover, ThemisGate, ThemisPolicy

PAGE = {"action": "page", "arguments": {"reason": "checkout down"}, "rationale": "needs a human", "confidence": 0.9}


class FakeThemis:
    """Themis's gate routes: the snapshot, policy reports and go / no-go requests."""

    def __init__(self):
        self.agents, self.quarantine, self.events, self.approvals = {}, {}, [], {}
        self.answer = "approved"  # what the person in the dashboard says
        self.up = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        if not self.up:
            raise httpx.ConnectError("Themis is down", request=request)
        path = request.url.path
        if path == "/v1/gate/snapshot":
            return httpx.Response(
                200, json={"agents": self.agents, "quarantine": self.quarantine, "fallbacks": {}, "faults": []}
            )
        if path == "/v1/gate/events":
            self.events += json.loads(request.content)["events"]
            return httpx.Response(202, json={"accepted": 1})
        if path == "/v1/approvals" and request.method == "POST":
            approval_id = "a%d" % len(self.approvals)
            self.approvals[approval_id] = {"request": json.loads(request.content), "polls": 0}
            return httpx.Response(201, json={"id": approval_id, "status": "pending"})
        if path.startswith("/v1/approvals/"):
            a = self.approvals[path.rsplit("/", 1)[1]]
            a["polls"] += 1
            return httpx.Response(200, json={"status": self.answer if a["polls"] > 1 else "pending"})
        return httpx.Response(404)


@pytest.fixture
def themis():
    return FakeThemis()


@pytest.fixture
def gate(themis):
    g = ThemisGate("http://themis:8600", "k", refresh_s=0.05, transport=httpx.MockTransport(themis))
    yield g.start()
    g.close()


def agent(gate, decision=ROLLBACK, approver=None, **policy):
    return Agent(
        "deployer",
        model=brain(decision),
        tools=[get_errors, rollback, page],
        policy=ThemisPolicy(gate=gate, **policy),
        approver=approver or ThemisApprover(gate, poll_s=0.01),
    )


def gate_spans(telemetry):
    tel, spans, _, _ = telemetry
    tel.force_flush()
    return [s for s in spans.get_finished_spans() if "themis.gate.decision" in (s.attributes or {})]


def test_autonomous_agents_follow_your_policy_alone(gate, themis):
    themis.agents["deployer"] = {"autonomy": "autonomous", "tools": [], "handoffs": [], "high_risk": []}
    gate.sync()
    run = agent(gate, PAGE).run("checkout is failing")
    assert run.outcome.status == "done" and themis.approvals == {}  # page needs no approval by your Policy


def test_supervised_agents_need_a_person_for_every_action(gate, themis, telemetry):
    themis.agents["deployer"] = {"autonomy": "supervised", "tools": [], "handoffs": [], "high_risk": []}
    gate.sync()
    run = agent(gate, PAGE).run("checkout is failing")
    assert run.verdict.requires_approval and "under supervision" in run.verdict.reasons[-1]
    (asked,) = themis.approvals.values()
    assert asked["request"]["agent"] == "deployer" and asked["request"]["action"] == "tool:page"
    assert run.outcome.status == "done" and run.outcome.approval.by == "themis"


def test_a_rejection_in_themis_stops_the_action_and_blames_the_agent(gate, themis, telemetry):
    themis.agents["deployer"] = {"autonomy": "supervised", "tools": [], "handoffs": [], "high_risk": []}
    themis.answer = "denied"
    gate.sync()
    run = agent(gate).run("checkout is failing")
    assert (
        run.status == "escalated" and run.outcome.status == "denied" and "rejected in Themis" in run.outcome.reasons[0]
    )
    (span,) = gate_spans(telemetry)
    assert span.attributes["themis.gate.decision"] == "denied_by_human"
    assert span.attributes["gen_ai.tool.name"] == "rollback" and not span.status.is_ok


def test_what_goes_to_themis_for_approval_is_redacted(gate, themis):
    themis.agents["deployer"] = {"autonomy": "supervised", "tools": [], "handoffs": [], "high_risk": []}
    gate.sync()
    value = "x" * 12  # put together here so no credential-shaped text sits in the repo
    leaky = {**ROLLBACK, "arguments": {"service": "checkout", "version": "pass" + "word=" + value}}
    agent(gate, leaky).run("x")
    (asked,) = themis.approvals.values()
    assert value not in json.dumps(asked) and "<redacted>" in asked["request"]["detail"]["arguments"]


def test_a_suspended_agent_reads_but_every_action_goes_to_a_person(gate, themis, telemetry):
    themis.agents["deployer"] = {"autonomy": "suspended", "tools": [], "handoffs": [], "high_risk": []}
    gate.sync()
    run = agent(gate, approver=AutoApprove()).run("checkout is failing")
    assert run.observations and run.observations[0].tool == "get_errors"  # reading is still allowed
    assert run.decision.action == "escalate" and run.outcome.status == "escalated"
    assert "suspended deployer" in run.decision.rationale
    assert [s.attributes["themis.gate.decision"] for s in gate_spans(telemetry)] == ["deny"]
    assert themis.events == []  # Themis's own decision is not a violation by the agent


def test_a_tool_outside_the_allow_list_is_blocked_and_reported(gate, themis):
    themis.agents["deployer"] = {
        "autonomy": "autonomous",
        "tools": ["get_errors", "page"],
        "handoffs": [],
        "high_risk": [],
    }
    gate.sync()
    run = agent(gate, approver=AutoApprove()).run("x")
    assert run.outcome.status == "escalated" and "not in deployer's allow-list" in run.decision.rationale
    gate.sync()
    assert themis.events == [
        {
            "agent": "deployer",
            "action": "tool:rollback",
            "decision": "deny",
            "reason": "rollback is not in deployer's allow-list in Themis",
        }
    ]


def test_a_quarantined_tool_is_blocked_until_the_quarantine_ends(gate, themis):
    themis.quarantine["tool:rollback"] = time.time() + 60
    gate.sync()
    assert agent(gate, approver=AutoApprove()).run("x").outcome.status == "escalated"
    themis.quarantine["tool:rollback"] = time.time() - 1
    gate.sync()
    assert agent(gate, approver=AutoApprove()).run("x").outcome.status == "done"


def test_themis_down_keeps_the_last_snapshot_and_the_reports(gate, themis):
    themis.agents["deployer"] = {"autonomy": "suspended", "tools": ["page"], "handoffs": [], "high_risk": []}
    gate.sync()
    themis.up = False
    gate.report("deployer", "tool:rollback", "deny", "outside")
    with pytest.raises(httpx.HTTPError):
        gate.sync()
    assert gate.autonomy("deployer") == "suspended"  # still the last word from Themis
    themis.up = True
    gate.sync()
    assert [e["action"] for e in themis.events] == ["tool:rollback"]


def test_no_answer_from_themis_is_a_no(gate, themis):
    themis.agents["deployer"] = {"autonomy": "supervised", "tools": [], "handoffs": [], "high_risk": []}
    gate.sync()
    themis.up = False
    run = agent(gate).run("x")
    assert run.outcome.status == "denied" and "could not be reached" in run.outcome.reasons[0]
    themis.up, themis.answer = True, "pending"
    slow = agent(gate, approver=ThemisApprover(gate, timeout_s=0.1, poll_s=0.01)).run("x")
    assert slow.outcome.status == "denied" and "nobody answered" in slow.outcome.reasons[0]


def test_the_gate_refreshes_by_itself(gate, themis):
    themis.agents["deployer"] = {"autonomy": "supervised", "tools": [], "handoffs": [], "high_risk": []}
    deadline = time.time() + 2
    while gate.autonomy("deployer") != "supervised" and time.time() < deadline:
        time.sleep(0.02)
    assert gate.autonomy("deployer") == "supervised"


def test_without_a_gate_it_is_a_plain_policy():
    run = Agent(
        "t", model=brain(ROLLBACK), tools=[get_errors, rollback, page], policy=ThemisPolicy(), approver=AutoApprove()
    )
    assert run.run("x").outcome.status == "done"


def test_from_env(monkeypatch):
    monkeypatch.delenv("THEMIS_URL", raising=False)
    assert ThemisGate.from_env() is None
    monkeypatch.setenv("THEMIS_URL", "http://themis:8600")
    monkeypatch.setenv("THEMIS_INGEST_KEY", "k")
    gate = ThemisGate.from_env()
    assert gate.url == "http://themis:8600"
    gate.close()
