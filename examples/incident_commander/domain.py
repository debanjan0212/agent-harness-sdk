"""Incident domain: what the product adds on top of the SDK's generic Analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent_harness_sdk import Analysis


class IncidentAnalysis(Analysis):
    """The SDK validates the model's output against this schema and retries until it fits."""

    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] = Field("SEV3", description="SEV1 = customers broadly down")
    impacted_services: list[str] = Field(default_factory=list)
    customer_impact: str = Field("", description="what customers experience, in plain words")
    suspect_change: str = Field("", description="deploy/config change that likely caused it, if any")


PLAYBOOK = """You are Incident Commander, the on-call SRE for an online shop running on Kubernetes.
Investigate like a senior SRE:
1. Check the health of the alerting service, then follow its dependencies to find where errors start.
2. For the suspect service, check recent deploys and search its logs.
3. Classify severity: SEV1 most customers affected, SEV2 a key flow broken, SEV3 degraded, SEV4 minor.
4. Mitigate first, investigate later: roll back a recent bad deploy, scale out only for pure saturation,
   escalate when a fix could make things worse (for example database exhaustion) or evidence is thin."""
