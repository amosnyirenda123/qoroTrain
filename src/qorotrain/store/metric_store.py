"""SQLite store for runs and their metric streams.

The Tracker already sends micro-batches, so one MQTT message is one ``executemany``
here. The composite key ``(run_id, name, step)`` makes redelivered messages (QoS 1 is
at-least-once) harmless: duplicates are ignored.

SQLite stores NaN as NULL, so a NULL value reads back as NaN. That keeps divergence visible.
"""
import json
import math
import sqlite3
import threading
import time
from typing import Iterable

from ..config.contracts.schema import MetricPoint

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    params      TEXT NOT NULL,
    status      TEXT NOT NULL,
    objective   REAL,
    error       TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL,
    name   TEXT NOT NULL,
    step   INTEGER NOT NULL,
    value  REAL,
    ts     REAL,
    PRIMARY KEY (run_id, name, step)
) WITHOUT ROWID;
"""


class MetricStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "MetricStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------- runs

    def create_run(self, run_id: str, project: str, params: dict, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, project, params, status, created_at) VALUES (?, ?, ?, 'created', ?)",
                (run_id, project, json.dumps(params), time.time() if ts is None else ts),
            )
            self._conn.commit()

    def mark_started(self, run_id: str, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE run_id = ? AND status = 'created'",
                (time.time() if ts is None else ts, run_id),
            )
            self._conn.commit()

    def finish_run(
        self, run_id: str, status: str, objective: float | None, error: str | None, ts: float | None = None
    ) -> None:
        if objective is not None and not math.isfinite(objective):
            objective = None
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, objective = ?, error = ?, finished_at = ? WHERE run_id = ?",
                (status, objective, error, time.time() if ts is None else ts, run_id),
            )
            self._conn.commit()

    def run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_dict(row) if row else None

    def runs(self, project: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM runs", ()
        if project is not None:
            sql, args = sql + " WHERE project = ?", (project,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, run_id", args).fetchall()
        return [_run_dict(r) for r in rows]

    # ---------------------------------------------------------------- metrics

    def add_metrics(self, run_id: str, points: Iterable[MetricPoint]) -> int:
        """Insert points in one transaction. Returns how many were new (duplicates are ignored)."""
        rows = [
            (run_id, name, point.step, None if math.isnan(value) else value, point.t)
            for point in points
            for name, value in point.values.items()
        ]
        if not rows:
            return 0
        with self._lock:
            cursor = self._conn.executemany("INSERT OR IGNORE INTO metrics VALUES (?, ?, ?, ?, ?)", rows)
            self._conn.commit()
            return cursor.rowcount

    def series(self, run_id: str, metric: str) -> list[tuple[int, float]]:
        """(step, value) pairs of one metric, ordered by step. NaN is returned for NULL."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT step, value FROM metrics WHERE run_id = ? AND name = ? ORDER BY step", (run_id, metric)
            ).fetchall()
        return [(r["step"], math.nan if r["value"] is None else r["value"]) for r in rows]

    def metric_names(self, run_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT name FROM metrics WHERE run_id = ? ORDER BY name", (run_id,)
            ).fetchall()
        return [r["name"] for r in rows]


def _run_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["params"] = json.loads(data["params"])
    return data