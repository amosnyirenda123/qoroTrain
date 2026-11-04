import sqlite3
import threading
from datetime import datetime, timezone

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
    return datetime.now(timezone.utc).isoformat()


class BridgeTable:
    """Durable view of what the bridge is connected to and listening on.

    The in-memory route list is what the bridge matches against; this table
    mirrors it for inspection and audit. Handlers are Python callables and
    cannot be stored, so the code stays the source of truth: ``open()`` clears
    the routes table and resets connector rows left over from a crashed process.

    Use one table file per running bridge.
    """

    def __init__(self, path: str = "bridge.db") -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
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
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------- connectors

    def set_connector(self, connector_id: str, status: str, kind: str | None = None) -> None:
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

    def connectors(self) -> list[dict]:
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
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute("DELETE FROM routes WHERE route_id = ?", (route.route_id,))
            self._conn.commit()

    def routes(self) -> list[dict]:
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT route_id, topic, handler, qos, created_at FROM routes ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]