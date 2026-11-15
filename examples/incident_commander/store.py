"""Incident store (SQLite). Swap for Postgres, Jira, PagerDuty or ServiceNow by keeping these methods."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
create table if not exists incidents (
  id text primary key, title text, service text, severity text, status text,
  opened_at real, mitigated_at real, summary text, cause text, action text, action_status text,
  analysis text, postmortem text);
create table if not exists timeline (incident_id text, ts real, kind text, detail text);
"""

# The incident columns update() may set. Anything else is refused before it reaches SQL.
UPDATABLE = frozenset(
    {
        "title",
        "service",
        "severity",
        "status",
        "mitigated_at",
        "summary",
        "cause",
        "action",
        "action_status",
        "analysis",
        "postmortem",
    }
)


class IncidentStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def open(self, title: str, service: str) -> str:
        iid = "INC-" + uuid.uuid4().hex[:6].upper()
        with self.lock:
            self.db.execute(
                "insert into incidents (id, title, service, severity, status, opened_at) values (?,?,?,?,?,?)",
                (iid, title, service, "SEV?", "investigating", time.time()),
            )
            self.db.commit()
        self.add(iid, "opened", title)
        return iid

    def add(self, iid: str, kind: str, detail: str) -> None:
        with self.lock:
            self.db.execute("insert into timeline values (?,?,?,?)", (iid, time.time(), kind, detail))
            self.db.commit()

    def update(self, iid: str, **fields) -> None:
        if not fields:
            return
        # Column names cannot be bound as parameters, so they are checked against the schema instead.
        # Values are always bound.
        unknown = sorted(set(fields) - UPDATABLE)
        if unknown:
            raise ValueError("unknown incident field(s): " + ", ".join(unknown))
        cols = ", ".join(k + " = ?" for k in fields)
        values = [json.dumps(v) if isinstance(v, dict | list) else v for v in fields.values()]
        with self.lock:
            self.db.execute("update incidents set " + cols + " where id = ?", [*values, iid])  # nosec B608
            self.db.commit()

    def get(self, iid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("select * from incidents where id = ?", (iid,)).fetchone()
            if row is None:
                return None
            events = self.db.execute(
                "select ts, kind, detail from timeline where incident_id = ? order by ts", (iid,)
            ).fetchall()
        out = dict(row)
        out["analysis"] = json.loads(out["analysis"]) if out["analysis"] else None
        out["timeline"] = [dict(e) for e in events]
        return out

    def list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "select id, title, service, severity, status, opened_at, action, action_status "
                "from incidents order by opened_at desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
