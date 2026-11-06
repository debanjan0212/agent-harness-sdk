"""OfflineBrain: a deterministic SRE playbook that speaks the same model protocol as DeepSeek.

It lets the product run with no API key (demos, CI, air-gapped clusters) and gives a baseline to compare
real models against. With DEEPSEEK_API_KEY set, the commander uses DeepSeek instead.
"""

from __future__ import annotations

import json
import re

from agent_harness_sdk import ModelReply, ToolCall

LINE = re.compile(r"^\[\d+\] ([a-z_]+)\((\{.*?\})\) -> (.*)$")


def _json(text: str):
    if text.startswith("WARNING:"):  # injection warning prefix added by the harness
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        text = text[min(starts) :] if starts else text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def evidence(messages: list[dict]) -> list[tuple[str, dict, object]]:
    """(tool, args, output) from tool-call transcripts (READ) or the evidence digest (ANALYSE/DECIDE)."""
    out, calls = [], {}
    for m in messages:
        if m["role"] == "assistant":
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"] or "{}"))
        elif m["role"] == "tool" and m.get("tool_call_id") in calls:
            name, args = calls[m["tool_call_id"]]
            out.append((name, args, _json(m["content"])))
        elif m["role"] == "user":
            for line in (m.get("content") or "").splitlines():
                hit = LINE.match(line)
                if hit:
                    out.append((hit.group(1), _json(hit.group(2)), _json(hit.group(3))))
    return out


def degraded(h: dict) -> bool:
    if not isinstance(h, dict) or "error_rate" not in h:
        return False
    err, base = h.get("error_rate") or 0, h.get("error_rate_1h_ago") or 0
    lat, lbase = h.get("p95_ms") or 0, h.get("p95_ms_1h_ago") or 0
    return err >= max(0.05, 5 * base) or (lbase > 0 and lat >= 2 * lbase) or (h.get("cpu_pct") or 0) >= 85


class OfflineBrain:
    model = "offline-sre-brain"

    def __call__(self, messages: list[dict], tools: list[dict] | None, json_mode: bool):
        system = messages[0]["content"]
        ev = evidence(messages)
        if "Stage READ" in system:
            return self.read(messages, ev)
        if "Stage ANALYSE" in system:
            return self.analyse(ev)
        return self.decide(ev)

    # ---------------------------------------------------------------- facts from evidence
    @staticmethod
    def facts(ev):
        health = {a.get("service"): o for t, a, o in ev if t == "service_health" and isinstance(o, dict)}
        deps = {a.get("service"): o for t, a, o in ev if t == "service_dependencies" and isinstance(o, list)}
        deploys = {a.get("service"): o for t, a, o in ev if t == "recent_deploys"}
        logs = {a.get("service"): o for t, a, o in ev if t == "search_logs"}
        return health, deps, deploys, logs

    @staticmethod
    def alert_service(messages) -> str:
        user = next(m["content"] for m in messages if m["role"] == "user")
        hit = re.search(r'"service":\s*"([^"]+)"', user)
        return hit.group(1) if hit else ""

    def suspect(self, health, deps, root):
        """Deepest degraded service along the dependency path from the alerting service."""
        order, frontier = [root], [root]
        while frontier:
            nxt = [d for s in frontier for d in deps.get(s, []) if d not in order]
            order += nxt
            frontier = nxt
        bad = [s for s in order if degraded(health.get(s, {}))]
        return bad[-1] if bad else root

    # ---------------------------------------------------------------- stages
    def read(self, messages, ev):
        root = self.alert_service(messages)
        health, deps, deploys, logs = self.facts(ev)

        def call(name, **args):
            return ToolCall("ob-" + name + "-" + "-".join(str(v) for v in args.values()), name, args)

        if root not in health:
            return ModelReply(
                None,
                [
                    call("service_health", service=root),
                    call("service_dependencies", service=root),
                    call("similar_incidents", query=root),
                ],
            )
        todo = [d for s in deps for d in deps[s] if d not in health]
        if todo:
            calls = []
            for d in todo:
                calls += [call("service_health", service=d), call("service_dependencies", service=d)]
            return ModelReply(None, calls)
        s = self.suspect(health, deps, root)
        if s not in deploys:
            return ModelReply(None, [call("recent_deploys", service=s), call("search_logs", service=s, minutes=30)])
        return "Evidence collected. Errors start at %s." % s

    def analyse(self, ev):
        health, deps, deploys, logs = self.facts(ev)
        root = next(iter(health), "")
        s = self.suspect(health, deps, root)
        h = health.get(s, {})
        err = h.get("error_rate") or 0
        recent = [d for d in deploys.get(s) or [] if isinstance(d, dict) and (d.get("minutes_ago") or 999) <= 60]
        patterns = (
            [p["pattern"] for p in (logs.get(s) or {}).get("top_patterns", [])] if isinstance(logs.get(s), dict) else []
        )
        findings = [
            "%s error rate %.1f%% (1h ago %.1f%%), p95 %sms"
            % (k, (v.get("error_rate") or 0) * 100, (v.get("error_rate_1h_ago") or 0) * 100, v.get("p95_ms"))
            for k, v in health.items()
            if isinstance(v, dict)
        ]
        if patterns:
            findings.append("top log pattern on %s: %s" % (s, patterns[0][:160]))
        cause, suspect_change = "", ""
        if recent:
            suspect_change = "%s %s deployed %s min ago (previous %s)" % (
                s,
                recent[0]["version"],
                recent[0]["minutes_ago"],
                recent[0].get("previous"),
            )
            cause = "release %s of %s introduced errors" % (recent[0]["version"], s)
        elif any(re.search(r"(?i)pool|connection", p) for p in patterns):
            cause = "%s is waiting on an exhausted database connection pool" % s
        elif (h.get("cpu_pct") or 0) >= 85:
            cause = "%s is CPU-saturated" % s
        if not any(degraded(v) for v in health.values()):
            return {
                "summary": "signals are back to normal; the alert has cleared",
                "findings": findings,
                "likely_cause": "",
                "confidence": 0.8,
                "severity": "SEV4",
                "impacted_services": [],
                "customer_impact": "none right now",
                "suspect_change": "",
            }
        sev = "SEV1" if err >= 0.25 else "SEV2" if err >= 0.1 else "SEV3"
        where = (
            ("%s errors trace down to %s" % (root, s))
            if s != root
            else ("%s is failing; its dependencies are healthy" % s)
        )
        return {
            "summary": where,
            "findings": findings,
            "likely_cause": cause,
            "confidence": 0.9 if recent else 0.75 if cause else 0.4,
            "severity": sev,
            "impacted_services": [k for k, v in health.items() if degraded(v)],
            "customer_impact": "about %d%% of %s requests fail" % (round(err * 100), s),
            "suspect_change": suspect_change,
        }

    def decide(self, ev):
        health, deps, deploys, logs = self.facts(ev)
        root = next(iter(health), "")
        s = self.suspect(health, deps, root)
        h = health.get(s, {})
        recent = [d for d in deploys.get(s) or [] if isinstance(d, dict) and (d.get("minutes_ago") or 999) <= 60]
        text = json.dumps(logs.get(s) or {})
        if not any(degraded(v) for v in health.values()):
            return {"action": "no_action", "rationale": "signals already recovered", "confidence": 0.8, "risk": "low"}
        if recent:
            return {
                "action": "rollback_deploy",
                "arguments": {"service": s, "to_version": recent[0]["previous"]},
                "rationale": "%s %s went out %s min ago and errors started right after; roll back to %s"
                % (s, recent[0]["version"], recent[0]["minutes_ago"], recent[0]["previous"]),
                "confidence": 0.9,
                "risk": "medium",
            }
        if re.search(r"(?i)pool|connection", text):
            return {
                "action": "escalate",
                "rationale": "database pool exhausted: scaling out would add connections and "
                "make it worse; needs a human",
                "confidence": 0.75,
                "risk": "high",
            }
        if (h.get("cpu_pct") or 0) >= 85:
            n = int(h.get("replicas") or 2)
            return {
                "action": "scale_service",
                "arguments": {"service": s, "replicas": n * 2},
                "rationale": "CPU saturation without errors in code",
                "confidence": 0.8,
                "risk": "medium",
            }
        return {
            "action": "escalate",
            "rationale": "no playbook rule matched the evidence",
            "confidence": 0.4,
            "risk": "medium",
        }
