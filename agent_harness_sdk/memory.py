"""Run memory: what the agent concluded before, so the next run can start from similar cases."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")


@dataclass
class MemoryRecord:
    run_id: str
    task: str
    summary: str
    action: str
    outcome: str
    tags: list[str]


class Memory(Protocol):
    def remember(self, record: MemoryRecord) -> None: ...

    def recall(self, query: str, k: int = 3) -> list[MemoryRecord]: ...


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class InMemoryMemory:
    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []
        self._lock = threading.Lock()

    def remember(self, record: MemoryRecord) -> None:
        with self._lock:
            self.records.append(record)

    def recall(self, query: str, k: int = 3) -> list[MemoryRecord]:
        q = _words(query)
        scored = [(len(q & _words(" ".join([r.task, r.summary, *r.tags]))), r) for r in self.records]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: -s[0])
        return [r for _, r in scored[:k]]


class JsonlMemory(InMemoryMemory):
    """Same keyword recall, persisted to a JSONL file. Swap in a vector store by implementing Memory."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self.records.append(MemoryRecord(**json.loads(line)))

    def remember(self, record: MemoryRecord) -> None:
        super().remember(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")
