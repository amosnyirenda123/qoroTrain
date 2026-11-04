import logging
import queue
import threading
from typing import Mapping, Sequence

from .table import BridgeTable
from ..connectors.config import BrokerConfig
from ..connectors.connector import Connector
from ..connectors.factory import create_connector
from ..messages.message import Message
from ..routes.route import Route
from ..tuners.tuner import Tuner

log = logging.getLogger(__name__)


class Bridge:
    """Coordinates connectors, routing and tuner communication.

    Incoming path:  connector (network thread) -> queue -> dispatch thread -> route handlers
    Outgoing path:  tuner -> ``Bridge.publish`` -> connector

    Connectors call back on the broker client's network thread, so the only
    work done there is putting the message on a queue. Handlers (which may be
    slow, e.g. an LLM call) run on a single dispatch thread, in arrival order.

    A message that matches several routes is delivered to all of them.
    When the queue is full, new messages are dropped and counted in ``dropped``.
    """
    
    def __init__(
        self,
        connectors: Mapping[str, Connector] | Sequence[Connector],
        routes: Sequence[Route] | None = None,
        tuner: Tuner | None = None,
        db_path: str = "bridge.db",
        queue_size: int = 10_000,
    ) -> None:
        if isinstance(connectors, Mapping):
            self._connectors = dict(connectors)
        else:
            self._connectors = {f"connector-{i}": c for i, c in enumerate(connectors)}
        if not self._connectors:
            raise ValueError("At least one connector is required.")

        self._routes: list[Route] = list(routes or [])
        self._subscribed: dict[str, int | None] = {}
        self._lock = threading.RLock()

        self._table = BridgeTable(db_path)
        self._queue_size = queue_size
        self._queue: queue.Queue[Message] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0

        self._tuner: Tuner | None = None
        self._tuner_routes: list[Route] = []
        self._tuner_started = False

        self._started = False

        if tuner is not None:
            self.attach(tuner)

    @classmethod
    def from_config(
        cls, config: BrokerConfig | Sequence[BrokerConfig], **kwargs
    ) -> "Bridge":
        configs = list(config) if isinstance(config, (list, tuple)) else [config]
        return cls([create_connector(c) for c in configs], **kwargs)

    # ------------------------------------------------------------ properties

    @property
    def started(self) -> bool:
        return self._started

    @property
    def dropped(self) -> int:
        """Messages dropped because the dispatch queue was full."""
        return self._dropped

    @property
    def routes(self) -> list[Route]:
        with self._lock:
            return list(self._routes)

    def status(self) -> list[dict]:
        """Connector rows from the bridge table (empty when the bridge is closed)."""
        return self._table.connectors()

    def routing_table(self) -> list[dict]:
        """Route rows from the bridge table (empty when the bridge is closed)."""
        return self._table.routes()

    # --------------------------------------------------------------- routing

    def add_route(self, route: Route) -> Route:
        with self._lock:
            if route in self._routes:
                return route
            self._routes.append(route)
            if self._started:
                self._table.add_route(route)
                self._sync_subscription(route.topic)
        return route

    def drop_route(self, route: Route) -> bool:
        """Remove a route. The broker subscription is dropped with the last route using it."""
        with self._lock:
            if route not in self._routes:
                return False
            self._routes.remove(route)
            if self._started:
                self._table.drop_route(route)
                if any(r.topic == route.topic for r in self._routes):
                    self._sync_subscription(route.topic)  # the QoS may have changed
                else:
                    for connector in self._connectors.values():
                        connector.unsubscribe(route.topic)
                    self._subscribed.pop(route.topic, None)
        return True

    def _qos_for(self, topic: str) -> int | None:
        explicit = [r.qos for r in self._routes if r.topic == topic and r.qos is not None]
        return max(explicit) if explicit else None

    def _sync_subscription(self, topic: str) -> None:
        """Subscribe on every connector, but only if the topic or its QoS is new."""
        qos = self._qos_for(topic)
        if topic in self._subscribed and self._subscribed[topic] == qos:
            return
        for connector in self._connectors.values():
            connector.subscribe(topic, qos)
        self._subscribed[topic] = qos

    # ----------------------------------------------------------------- tuner

    def attach(self, tuner: Tuner) -> "Bridge":
        if self._tuner is not None:
            raise RuntimeError("A tuner is already attached; detach it first.")
        self._tuner = tuner
        self._tuner_routes = [self.add_route(r) for r in tuner.routes()]
        if self._started:
            tuner.start(self)
            self._tuner_started = True
        return self

    def detach(self) -> None:
        tuner = self._tuner
        if tuner is None:
            return
        for route in self._tuner_routes:
            self.drop_route(route)
        if self._tuner_started:
            tuner.stop()
        self._tuner = None
        self._tuner_routes = []
        self._tuner_started = False

    # ------------------------------------------------------------- messaging

    def publish(self, message: Message, connector_id: str | None = None) -> None:
        if not self._started:
            raise RuntimeError("Bridge is not running.")
        if connector_id is None:
            connector = next(iter(self._connectors.values()))
        else:
            try:
                connector = self._connectors[connector_id]
            except KeyError:
                raise KeyError(f"Unknown connector_id: {connector_id}") from None
        connector.publish(message)

    def _enqueue(self, message: Message) -> None:
        """Runs on the broker client's network thread: enqueue and return."""
        q = self._queue
        if q is None:
            return
        try:
            q.put_nowait(message)
        except queue.Full:
            self._dropped += 1
            log.warning("Dispatch queue full; dropping message on %s", message.topic)

    def _run(self) -> None:
        q = self._queue
        while not self._stop.is_set():
            try:
                message = q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._dispatch(message)

    def _dispatch(self, message: Message) -> None:
        with self._lock:
            routes = [r for r in self._routes if r.matches(message.topic)]
        if not routes:
            log.debug("No route for %s", message.topic)
            return
        for route in routes:
            try:
                route.handler(message)
            except Exception:
                log.exception("Handler %s failed for %s", route.handler_name, message.topic)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "Bridge":
        if self._started:
            raise RuntimeError("Bridge is already running.")

        self._stop.clear()
        self._dropped = 0
        self._queue = queue.Queue(maxsize=self._queue_size)
        self._table.open()
        self._thread = threading.Thread(target=self._run, name="bridge-dispatch", daemon=True)
        self._thread.start()

        try:
            for connector_id, connector in self._connectors.items():
                kind = type(connector).__name__
                self._table.set_connector(connector_id, "registered", kind)
                connector.on_message(self._enqueue)
                try:
                    connector.connect()
                except Exception:
                    self._table.set_connector(connector_id, "error", kind)
                    raise
                self._table.set_connector(connector_id, "running", kind)

            with self._lock:
                for route in self._routes:
                    self._table.add_route(route)
                for topic in dict.fromkeys(r.topic for r in self._routes):
                    self._sync_subscription(topic)
                self._started = True

            if self._tuner is not None:
                self._tuner.start(self)
                self._tuner_started = True
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Stop everything. Safe to call more than once, and after a failed start."""
        self._stop.set()

        for connector_id, connector in self._connectors.items():
            try:
                connector.disconnect()
            except Exception:
                log.exception("Error closing connector %s.", connector_id)
            self._table.set_connector(connector_id, "stopped")

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning("Dispatch thread did not stop in time (a handler is still running).")

        if self._tuner is not None and self._tuner_started:
            try:
                self._tuner.stop()
            except Exception:
                log.exception("Error stopping tuner.")
            self._tuner_started = False

        self._table.close()
        with self._lock:
            self._subscribed.clear()
            self._started = False
        self._queue = None
        self._thread = None

    def __enter__(self) -> "Bridge":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False