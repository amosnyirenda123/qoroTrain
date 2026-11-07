"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/store/metric_store.py

Description:
    SQLite store for runs and their metric streams.

    The Tracker already sends micro-batches, so one MQTT message is one ``executemany``
    here. The composite key ``(run_id, name, step)`` makes redelivered messages (QoS 1 is
    at-least-once) harmless: duplicates are ignored.

    SQLite stores NaN as NULL, so a NULL value reads back as NaN. That keeps divergence visible.

    Schema:
        ``runs(run_id PK, project, params JSON, status, objective, error,
        created_at, started_at, finished_at)``, timestamps as Unix seconds;
        ``metrics(run_id, name, step, value, ts)`` with primary key
        ``(run_id, name, step)``, stored ``WITHOUT ROWID`` because rows are
        always looked up by that key.

    Run lifecycle:
        Values of ``runs.status``: ``created`` (start command sent) -> ``running`` (Runner reported
        ``started``) -> ``finished`` | ``failed`` | ``stopped`` (Tuner scored
        the run).

    Concurrency:
        One connection shared across threads (``check_same_thread=False``),
        serialised by a lock. Route handlers on the Bridge's dispatch thread
        write metrics while the Tuner's decision thread reads series. WAL
        journaling lets other processes read the file concurrently.

===============================================================================
"""
import json
import math
import sqlite3
import threading
import time
from typing import Any, Iterable

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
    """Thread-safe SQLite persistence for runs and metric points.

    Runs from earlier sessions are kept; nothing is deleted automatically.
    Usable as a context manager, which closes the connection on exit.

    Methods other than :meth:`close` must not be called after the store is
    closed (the connection is gone and an ``AttributeError`` results).
    """

    def __init__(self, path: str = ":memory:") -> None:
        """Open (or create) the database and its schema.

        Args:
            path: SQLite database file; the default in-memory database is
                convenient for tests. The parent directory must exist.

        Raises:
            sqlite3.Error: If the database cannot be opened.
        """
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "MetricStore":
        """Return the store itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the store."""
        self.close()

    # ------------------------------------------------------------------- runs

    def create_run(self, run_id: str, project: str, params: dict[str, Any], ts: float | None = None) -> None:
        """Record a new run in state ``created``.

        Args:
            run_id: Unique run identifier.
            project: Experiment name.
            params: Parameters the run is launched with; stored as JSON.
            ts: Creation time (Unix seconds); defaults to now.

        Raises:
            sqlite3.IntegrityError: If ``run_id`` already exists.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, project, params, status, created_at) VALUES (?, ?, ?, 'created', ?)",
                (run_id, project, json.dumps(params), time.time() if ts is None else ts),
            )
            self._conn.commit()

    def mark_started(self, run_id: str, ts: float | None = None) -> None:
        """Move a run from ``created`` to ``running``.

        Has no effect if the run is unknown or already past ``created``, which
        makes duplicate ``started`` statuses harmless.

        Args:
            run_id: Run identifier.
            ts: Start time (Unix seconds); defaults to now.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'running', started_at = ? WHERE run_id = ? AND status = 'created'",
                (time.time() if ts is None else ts, run_id),
            )
            self._conn.commit()

    def finish_run(
        self, run_id: str, status: str, objective: float | None, error: str | None, ts: float | None = None
    ) -> None:
        """Record a run's final state and score.

        Args:
            run_id: Run identifier.
            status: Terminal status (``finished``, ``failed`` or ``stopped``).
            objective: The run's score. Non-finite values are stored as NULL.
            error: Why the run has no objective, if applicable.
            ts: Finish time (Unix seconds); defaults to now.
        """
        if objective is not None and not math.isfinite(objective):
            objective = None
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, objective = ?, error = ?, finished_at = ? WHERE run_id = ?",
                (status, objective, error, time.time() if ts is None else ts, run_id),
            )
            self._conn.commit()

    def run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch one run.

        Args:
            run_id: Run identifier.

        Returns:
            The run's columns as a dictionary (``params`` decoded from JSON),
            or ``None`` if there is no such run.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_dict(row) if row else None

    def runs(self, project: str | None = None) -> list[dict[str, Any]]:
        """List runs in creation order.

        Args:
            project: Restrict to one experiment; ``None`` lists every run.

        Returns:
            One dictionary per run, as for :meth:`run`.
        """
        sql, args = "SELECT * FROM runs", ()
        if project is not None:
            sql, args = sql + " WHERE project = ?", (project,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, run_id", args).fetchall()
        return [_run_dict(r) for r in rows]

    # ---------------------------------------------------------------- metrics

    def add_metrics(self, run_id: str, points: Iterable[MetricPoint]) -> int:
        """Insert points in one transaction. Returns how many were new (duplicates are ignored).

        Each point expands into one row per metric name. NaN values are
        stored as NULL; infinities are stored as-is.

        Args:
            run_id: Run the points belong to.
            points: Metric points, typically one decoded
                :class:`~qorotrain.config.contracts.schema.MetricBatch`.

        Returns:
            The number of rows actually inserted.
        """
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
        """(step, value) pairs of one metric, ordered by step. NaN is returned for NULL.

        Args:
            run_id: Run identifier.
            metric: Metric name.

        Returns:
            The metric's points in step order; empty if none were logged.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT step, value FROM metrics WHERE run_id = ? AND name = ? ORDER BY step", (run_id, metric)
            ).fetchall()
        return [(r["step"], math.nan if r["value"] is None else r["value"]) for r in rows]

    def metric_names(self, run_id: str) -> list[str]:
        """Names of the metrics a run has logged, sorted alphabetically.

        Args:
            run_id: Run identifier.

        Returns:
            Distinct metric names.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT name FROM metrics WHERE run_id = ? ORDER BY name", (run_id,)
            ).fetchall()
        return [r["name"] for r in rows]


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``runs`` row to a dictionary, decoding the ``params`` JSON."""
    data = dict(row)
    data["params"] = json.loads(data["params"])
    return data
