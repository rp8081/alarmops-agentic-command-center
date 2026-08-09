from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any


class TelemetryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, query TEXT, status TEXT, "
                "started_at TEXT, finished_at TEXT, duration_ms REAL, confidence TEXT, payload TEXT);"
                "CREATE TABLE IF NOT EXISTS spans (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
                "node TEXT, status TEXT, duration_ms REAL, detail TEXT, created_at TEXT);"
                "CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
                "decision TEXT, approved_by TEXT, created_at TEXT);"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def start(self, run_id: str, query: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)",
                (run_id, query, "running", datetime.now(UTC).isoformat(), "{}"),
            )

    def span(self, run_id: str, node: str, status: str, duration_ms: float, detail: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO spans(run_id,node,status,duration_ms,detail,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, node, status, duration_ms, json.dumps(detail, default=str), datetime.now(UTC).isoformat()),
            )

    def finish(self, run_id: str, status: str, payload: Any, confidence: str | None = None) -> None:
        with self._connect() as connection:
            started = connection.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
            started_at = datetime.fromisoformat(started["started_at"]) if started else datetime.now(UTC)
            elapsed = (datetime.now(UTC) - started_at).total_seconds() * 1000
            connection.execute(
                "UPDATE runs SET status=?,finished_at=?,duration_ms=?,confidence=?,payload=? WHERE run_id=?",
                (status, datetime.now(UTC).isoformat(), elapsed, confidence, json.dumps(payload, default=str), run_id),
            )

    def approval(self, run_id: str, decision: str, approved_by: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals(run_id,decision,approved_by,created_at) VALUES(?,?,?,?)",
                (run_id, decision, approved_by, datetime.now(UTC).isoformat()),
            )

    def runs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            spans = connection.execute("SELECT * FROM spans WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            approvals = connection.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return {**dict(run), "spans": [dict(row) for row in spans], "approvals": [dict(row) for row in approvals]} if run else None

    def metrics(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) count, AVG(duration_ms) avg_ms, SUM(status='completed') completed, "
                "SUM(status='degraded') degraded FROM runs"
            ).fetchone()
            tools = connection.execute(
                "SELECT node, COUNT(*) calls, AVG(duration_ms) avg_ms FROM spans GROUP BY node"
            ).fetchall()
        return {"runs": dict(row), "nodes": [dict(item) for item in tools]}


class SpanTimer:
    def __init__(self) -> None:
        self.started = perf_counter()

    @property
    def milliseconds(self) -> float:
        return round((perf_counter() - self.started) * 1000, 2)
