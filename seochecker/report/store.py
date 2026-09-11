"""SQLite history, so consecutive crawls can be compared.

The question this answers is "what changed since last week", which no single
report can. Findings are stored per run, keyed by finding id and URL, so a diff
is a set operation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..models import Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    pages       INTEGER NOT NULL,
    score       REAL,
    grade       TEXT,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    finding_id  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    category    TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS findings_by_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS runs_by_target ON runs(target, started_at);
"""


@dataclass(slots=True)
class RunRecord:
    id: int
    target: str
    started_at: str
    pages: int
    score: float | None
    grade: str | None


@dataclass(slots=True)
class RunDiff:
    previous: RunRecord
    current: RunRecord
    introduced: list[tuple[str, str, str]] = field(default_factory=list)   # (severity, id, url)
    resolved: list[tuple[str, str, str]] = field(default_factory=list)
    score_change: float = 0.0

    @property
    def unchanged(self) -> bool:
        return not self.introduced and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_run": {"id": self.previous.id, "at": self.previous.started_at,
                             "score": self.previous.score},
            "current_run": {"id": self.current.id, "at": self.current.started_at,
                            "score": self.current.score},
            "score_change": round(self.score_change, 1),
            "introduced": [{"severity": s, "finding_id": f, "url": u}
                           for s, f, u in self.introduced],
            "resolved": [{"severity": s, "finding_id": f, "url": u}
                         for s, f, u in self.resolved],
        }


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def record(self, *, target: str, pages: int, findings: Iterable[Finding],
               score: float | None = None, grade: str | None = None,
               summary: dict[str, Any] | None = None) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs (target, started_at, pages, score, grade, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             pages, score, grade, json.dumps(summary or {})),
        )
        run_id = int(cursor.lastrowid)
        self.connection.executemany(
            "INSERT INTO findings (run_id, finding_id, severity, category, url, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, f.id, f.severity.value, f.category, f.url or "", f.message)
             for f in findings],
        )
        self.connection.commit()
        return run_id

    def runs_for(self, target: str, limit: int = 10) -> list[RunRecord]:
        rows = self.connection.execute(
            "SELECT id, target, started_at, pages, score, grade FROM runs "
            "WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def findings_for(self, run_id: int) -> set[tuple[str, str, str]]:
        rows = self.connection.execute(
            "SELECT severity, finding_id, url FROM findings WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {(row["severity"], row["finding_id"], row["url"]) for row in rows}


def diff_runs(store: RunStore, target: str) -> RunDiff | None:
    """Compare the two most recent runs for a target, newest first."""
    runs = store.runs_for(target, limit=2)
    if len(runs) < 2:
        return None
    current, previous = runs[0], runs[1]
    now = store.findings_for(current.id)
    before = store.findings_for(previous.id)
    return RunDiff(
        previous=previous,
        current=current,
        introduced=sorted(now - before),
        resolved=sorted(before - now),
        score_change=(current.score or 0.0) - (previous.score or 0.0),
    )
