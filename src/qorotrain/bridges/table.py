"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/bridges/table.py

Description:
    SQLite mirror of the Bridge's connectors and routes ("routing table").

    The Bridge matches messages against its in-memory route list; this module
    persists a copy of that list, plus the status of each connector, to a
    small SQLite file (``bridge.db``, created next to the metric store by the
    CLI). The file exists for inspection and auditing (which handlers were
    listening on which topics, which connectors were up) and is never read
    back to restore state.

    Schema:
        ``connectors(connector_id PK, kind, status, created_at, last_checked)``
        where ``status`` is ``registered``, ``running``, ``error`` or
        ``stopped``; and ``routes(route_id PK, topic, handler, qos,
        created_at)``. Timestamps are ISO-8601 UTC strings.

    Concurrency:
        One ``sqlite3`` connection is shared across threads
        (``check_same_thread=False``) and serialised with a
        ``threading.Lock``. WAL journaling lets external readers inspect the
        file while the Bridge writes.

===============================================================================
"""
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from ..routes.route import Route

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connectors (
    connector_id TEXT PRIMARY KEY,
    kind         TEXT,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_checked TEXT
);
CREATE TABLE IF NOT EXISTS routes (
    route_id   TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    handler    TEXT NOT NULL,
    qos        INTEGER,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class BridgeTable:
    """Durable view of what the bridge is connected to and listening on.

    The in-memory route list is what the bridge matches against; this table
    mirrors it for inspection and audit. Handlers are Python callables and
    cannot be stored, so the code stays the source of truth: ``open()`` clears
    the routes table and resets connector rows left over from a crashed process.

    Use one table file per running bridge.

    Every write method is a no-op while the table is closed, so the Bridge can
    call them unconditionally (for example during a failed start).
    """

    def __init__(self, path: str = "bridge.db") -> None:
        """Prepare the table; no file is opened until :meth:`open`.

        Args:
            path: SQLite database file (``":memory:"`` works for tests).
        """
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        """Open the database, create the schema, and discard stale state.

        Connector rows not already ``stopped`` are marked ``stopped`` (they
        belong to a previous process that did not shut down cleanly) and all
        route rows are deleted. Calling ``open`` on an open table does nothing.

        Raises:
            sqlite3.Error: If the database cannot be opened or initialised.
        """
        with self._lock:
            if self._conn is not None:
                return
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute(
                "UPDATE connectors SET status = 'stopped', last_checked = ? "
                "WHERE status != 'stopped'",
                (_now(),),
            )
            conn.execute("DELETE FROM routes")
            conn.commit()
            self._conn = conn

    def close(self) -> None:
        """Close the database connection. Safe to call more than once."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------- connectors

    def set_connector(self, connector_id: str, status: str, kind: str | None = None) -> None:
        """Insert or update a connector's status (upsert).

        Args:
            connector_id: Bridge-assigned connector name.
            status: New status (``registered``, ``running``, ``error`` or
                ``stopped``).
            kind: Connector class name. ``None`` keeps the stored value.
        """
        now = _now()
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                """
                INSERT INTO connectors (connector_id, kind, status, created_at, last_checked)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    status = excluded.status,
                    last_checked = excluded.last_checked,
                    kind = COALESCE(excluded.kind, connectors.kind)
                """,
                (connector_id, kind, status, now, now),
            )
            self._conn.commit()

    def connectors(self) -> list[dict[str, Any]]:
        """Return every connector row, ordered by identifier.

        Returns:
            One dictionary per row (empty while the table is closed).
        """
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT connector_id, kind, status, created_at, last_checked "
                "FROM connectors ORDER BY connector_id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- routes

    def add_route(self, route: Route) -> None:
        """Record a route (replacing any row with the same ``route_id``).

        Args:
            route: The route registered with the Bridge.
        """
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO routes (route_id, topic, handler, qos, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (route.route_id, route.topic, route.handler_name, route.qos, _now()),
            )
            self._conn.commit()

    def drop_route(self, route: Route) -> None:
        """Delete a route's row.

        Args:
            route: The route removed from the Bridge.
        """
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM routes WHERE route_id = ?", (route.route_id,))
            self._conn.commit()

    def routes(self) -> list[dict[str, Any]]:
        """Return every route row, oldest first.

        Returns:
            One dictionary per row (empty while the table is closed).
        """
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT route_id, topic, handler, qos, created_at FROM routes ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
