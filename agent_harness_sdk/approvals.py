"""Human-in-the-loop. The harness asks an Approver before any approval-required action runs."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ApprovalRequest:
    run_id: str
    agent: str
    action: str
    arguments: dict
    rationale: str
    risk: str
    confidence: float
    summary: str


@dataclass
class ApprovalResponse:
    approved: bool
    by: str
    comment: str = ""


class Approver(Protocol):
    async def review(self, request: ApprovalRequest) -> ApprovalResponse: ...


class AutoApprove:
    """Approves everything. Only for tests, demos and actions you have already decided are safe."""

    async def review(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(True, "auto")


class DenyAll:
    async def review(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(False, "policy", "approvals are disabled")


class CallbackApprover:
    """Wraps any function(request) -> bool | ApprovalResponse (sync or async): Slack buttons, a ticket, a CLI prompt."""

    def __init__(self, fn: Callable[[ApprovalRequest], bool | ApprovalResponse | Awaitable], name: str = "callback"):
        self.fn, self.name = fn, name

    async def review(self, request: ApprovalRequest) -> ApprovalResponse:
        out = self.fn(request)
        if inspect.isawaitable(out):
            out = await out
        return out if isinstance(out, ApprovalResponse) else ApprovalResponse(bool(out), self.name)


class PendingApprovals:
    """Approver for web apps: review() parks the request until resolve() is called (e.g. from an HTTP endpoint)
    or the timeout passes, which counts as a denial."""

    def __init__(self, timeout_s: float = 900.0):
        self.timeout_s = timeout_s
        self.pending: dict[str, tuple[ApprovalRequest, asyncio.Future]] = {}

    async def review(self, request: ApprovalRequest) -> ApprovalResponse:
        fut = asyncio.get_running_loop().create_future()
        self.pending[request.run_id] = (request, fut)
        try:
            return await asyncio.wait_for(fut, self.timeout_s)
        except TimeoutError:
            return ApprovalResponse(False, "timeout", "no answer in %ss" % self.timeout_s)
        finally:
            self.pending.pop(request.run_id, None)

    def resolve(self, run_id: str, approved: bool, by: str, comment: str = "") -> bool:
        item = self.pending.get(run_id)
        if not item or item[1].done():
            return False
        item[1].set_result(ApprovalResponse(approved, by, comment))
        return True
