import asyncio
import importlib.util

import pytest
from incident_commander import IncidentCommander
from incident_commander.sources import FixtureSource

from agent_harness_sdk import AutoApprove, DenyAll, PendingApprovals


@pytest.mark.parametrize(
    "scenario,status,action,severity",
    [
        ("bad-deploy", "mitigated", "rollback_deploy", "SEV1"),
        ("db-pool", "escalated", "escalate", "SEV2"),
        ("injection", "resolved", "no_action", "SEV4"),
    ],
)
def test_scenarios(scenario, status, action, severity):
    source = FixtureSource(scenario)
    inc = asyncio.run(IncidentCommander(source=source, approver=AutoApprove()).handle(source.alert))
    assert (inc["status"], inc["action"], inc["severity"]) == (status, action, severity)
    assert "## Timeline" in inc["postmortem"] and inc["timeline"]
    if scenario == "bad-deploy":
        assert source.actions == [{"rollback": "checkout", "to": "1.3.2"}]
    else:
        assert source.actions == []


def test_denied_rollback_is_escalated_and_paged():
    source = FixtureSource("bad-deploy")
    inc = asyncio.run(IncidentCommander(source=source, approver=DenyAll()).handle(source.alert))
    assert inc["status"] == "escalated" and source.actions == []
    assert any(e["kind"] == "paged" for e in inc["timeline"])


def test_domain_rule_blocks_rollback_without_recent_deploy():
    from incident_commander.commander import rollback_needs_recent_deploy

    class D:
        action, arguments = "rollback_deploy", {"service": "checkout"}

    class R:
        observations = []

    assert "no recent deploy" in rollback_needs_recent_deploy(D(), R())


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="fastapi not installed")
def test_api_flow_with_human_approval():
    import httpx
    from incident_commander.server import create_app

    async def go():
        approvals = PendingApprovals(timeout_s=10)
        source = FixtureSource("bad-deploy")
        app = create_app(IncidentCommander(source=source, approver=approvals), approvals)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ic") as c:
            r = await c.post("/webhooks/alertmanager", json={"alerts": [source.alert]})
            assert r.json() == {"accepted": 1}
            for _ in range(200):
                pending = (await c.get("/approvals")).json()
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending[0]["action"] == "rollback_deploy"
            iid = pending[0]["incident"]
            assert (await c.post("/incidents/" + iid + "/approve", json={"by": "debanjan"})).status_code == 200
            for _ in range(200):
                inc = (await c.get("/incidents/" + iid)).json()
                if inc["status"] != "investigating":
                    break
                await asyncio.sleep(0.01)
            pm = (await c.get("/incidents/" + iid + "/postmortem")).text
            return inc, pm, source

    inc, pm, source = asyncio.run(go())
    assert inc["status"] == "mitigated" and source.actions
    assert any(e["detail"] == "approved by debanjan" for e in inc["timeline"]) and "# Postmortem draft" in pm
