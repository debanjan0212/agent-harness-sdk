"""Rules that hold even when the model is wrong or manipulated. They run in code, never in the prompt."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<aws-access-key>"),
    (re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), "<jwt>"),
    (re.compile(r"(?i)\b(bearer)\s+[\w.~+/-]{16,}=*"), r"\1 <token>"),
    (
        re.compile(r"""(?i)\b(password|passwd|secret|api[_-]?key|token|x-api-key)\b("?\s*[=:]\s*"?)[^\s"',;}]+"""),
        r"\1\2<redacted>",
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"), "<private-key>"),
    (re.compile(r"\bpat\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<harness-pat>"),
    # scheme://user:password@host, as in a database or broker connection string; the user name stays readable
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]*):[^\s/@]+@"), r"\1:<redacted>@"),
]
# 13 to 16 digits with single spaces or dashes between them, and nothing after the last digit.
# Only a candidate: millisecond timestamps and order ids have this shape too, so the checksum decides.
CARD = re.compile(r"\b\d(?:[ -]?\d){12,15}\b")
INJECTION = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) (instructions|prompts)|you are now|system (prompt|override)|"
    r"disregard .{0,30}(rules|instructions)|reveal .{0,20}(prompt|secrets?)|kubectl (delete|exec|apply)|"
    r"rm -rf|drop table|curl [^ ]+ \| ?(sh|bash))"
)


def luhn_ok(digits: str) -> bool:
    """The checksum every payment card number passes. About one random number in ten passes it by chance."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str) -> tuple[str, int]:
    count = 0
    for rx, repl in SECRET_PATTERNS:
        text, n = rx.subn(repl, text)
        count += n

    cards = 0

    def mask_card(m: re.Match) -> str:
        nonlocal cards
        if not luhn_ok(re.sub(r"[ -]", "", m.group(0))):
            return m.group(0)
        cards += 1
        return "<card-number>"

    text = CARD.sub(mask_card, text)
    return text, count + cards


def find_injection(text: str) -> str | None:
    m = INJECTION.search(text)
    return m.group(0) if m else None


@dataclass
class Budget:
    max_read_steps: int = 8  # model turns in the READ stage
    max_tool_calls: int = 25
    max_output_chars: int = 8000  # per tool result sent to the model
    max_tokens: int = 200_000  # input + output across the run
    run_timeout_s: float = 300.0


@dataclass
class PolicyVerdict:
    allowed: bool
    requires_approval: bool
    reasons: list[str] = field(default_factory=list)


# A custom rule returns a reason to block (str) or None to pass.
Rule = Callable[["object", "object"], "str | None"]


@dataclass
class Policy:
    """Decides whether a proposed action may run. Anything not allowed becomes an escalation to a human."""

    min_confidence: float = 0.7
    approval_for: str = "mutating"  # "mutating" | "high_risk" | "never" | "always"
    deny_actions: set[str] = field(default_factory=set)
    max_actions_per_run: int = 1
    dry_run: bool = False
    block_on_injection: bool = True
    rules: list[Rule] = field(default_factory=list)

    def evaluate(self, decision, run) -> PolicyVerdict:
        reasons = []
        tool = run.agent.tools.get(decision.action)
        if decision.action in ("no_action", "escalate"):
            return PolicyVerdict(True, False)
        if tool is None or not tool.mutating:
            reasons.append("unknown action %r" % decision.action)
        if decision.action in self.deny_actions:
            reasons.append("action %r is denied by policy" % decision.action)
        if decision.confidence < self.min_confidence:
            reasons.append("confidence %.2f is below the %.2f minimum" % (decision.confidence, self.min_confidence))
        if self.block_on_injection and run.flags:
            reasons.append("possible prompt injection was seen in tool output; a human must review")
        if len(run.actions) >= self.max_actions_per_run:
            reasons.append("action limit of %d per run reached" % self.max_actions_per_run)
        if tool is not None:
            try:
                tool.validate(decision.arguments)
            except Exception as exc:  # noqa: BLE001 - invalid arguments are a policy failure, not a crash
                reasons.append(str(exc))
        for rule in self.rules:
            why = rule(decision, run)
            if why:
                reasons.append(why)
        if reasons:
            return PolicyVerdict(False, False, reasons)
        approval = {
            "never": False,
            "always": True,
            "high_risk": decision.risk == "high" or bool(tool.requires_approval and decision.risk != "low"),
            "mutating": bool(tool.requires_approval) or decision.risk == "high",
        }[self.approval_for]
        return PolicyVerdict(True, approval)
