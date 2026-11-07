"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/bridges/bridge.py

Description:
    The Bridge: message hub between broker connectors and the components
    (Tuner, Runner) of a tuning session.

    The Bridge owns a pool of :class:`~qorotrain.connectors.connector.Connector`
    objects, a list of :class:`~qorotrain.routes.route.Route` objects, and the
    :class:`~qorotrain.bridges.component.Component` objects it hosts. It keeps
    broker subscriptions in sync with the routes, decouples the brokers'
    network threads from handler execution with a bounded queue, and mirrors
    its state into a :class:`~qorotrain.bridges.table.BridgeTable`.

    Design:
        Mediator pattern. Components never talk to connectors or to each
        other; they register routes and publish through the Bridge. That is
        what lets the Tuner and the Runner run in one process today and in
        separate processes later without code changes.

    Threading:
        Exactly one dispatch thread (``bridge-dispatch``, daemon) runs every
        handler, in arrival order, so handlers never run concurrently with
        each other and need no locking among themselves. A slow handler delays
        all later messages, which is why the Tuner and Runner hand slow work
        to their own threads. Route and subscription bookkeeping is guarded by
        a re-entrant lock.

    Backpressure:
        The dispatch queue is bounded (``queue_size``, default 10,000). When
        it is full, new messages are dropped (never blocking the broker's
        network thread) and counted in :attr:`Bridge.dropped`.

===============================================================================
"""
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .component import Component
from .table import BridgeTable
from ..connectors.config import BrokerConfig
from ..connectors.connector import Connector
from ..connectors.factory import create_connector
from ..messages.message import Message
from ..routes.route import Route

log = logging.getLogger(__name__)


@dataclass
class _Attached:
    """Bookkeeping for a hosted component.

    Attributes:
        component (Component): The hosted component.
        routes (list[Route]): The routes registered on its behalf (removed on
            detach).
        started (bool): Whether ``component.start`` has been called and not
            yet matched by ``stop``.
    """

    component: Component
    routes: list[Route]
    started: bool = False


class Bridge:
    """Coordinates connectors, routing and the components it hosts (Tuner, Runner...).

    Incoming path:  connector (network thread) -> queue -> dispatch thread -> route handlers
    Outgoing path:  component -> ``Bridge.publish`` -> connector

    Connectors call back on the broker client's network thread, so the only
    work done there is putting the message on a queue. Handlers (which may be
    slow, e.g. an LLM call) run on a single dispatch thread, in arrival order.

    A message that matches several routes is delivered to all of them.
    When the queue is full, new messages are dropped and counted in ``dropped``.

    Every connector subscribes to every route's topic; published messages go
    through the first connector unless another is named. The Bridge can be
    restarted after :meth:`close`, and is usable as a context manager
    (``with bridge:`` calls :meth:`start` and :meth:`close`).

    Attributes:
        started (bool): Whether the Bridge is running (read-only).
        dropped (int): Messages dropped since the last start (read-only).
        routes (list[Route]): Snapshot of the registered routes (read-only).
    """

    def __init__(
        self,
        connectors: Mapping[str, Connector] | Sequence[Connector],
        routes: Sequence[Route] | None = None,
        components: Sequence[Component] | None = None,
        db_path: str = "bridge.db",
        queue_size: int = 10_000,
    ) -> None:
        """Configure the Bridge; nothing connects until :meth:`start`.

        Args:
            connectors: Connectors by identifier, or a sequence (identifiers
                become ``connector-0``, ``connector-1``...). The first one is
                the default for publishing.
            routes: Routes to register immediately.
            components: Components to attach immediately (their routes are
                registered; they are started with the Bridge).
            db_path: SQLite file for the routing table.
            queue_size: Capacity of the dispatch queue.

        Raises:
            ValueError: If no connector is given.
            RuntimeError: If the same component appears twice in
                ``components``.
        """
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

        self._attached: list[_Attached] = []

        self._started = False

        for component in components or []:
            self.attach(component)

    @classmethod
    def from_config(
        cls, config: BrokerConfig | Sequence[BrokerConfig], **kwargs: Any
    ) -> "Bridge":
        """Build a Bridge whose connectors are created from broker configurations.

        Args:
            config: One configuration, or a list/tuple of them.
            **kwargs: Passed to the constructor (``components``, ``db_path``...).

        Returns:
            A new, not yet started Bridge.

        Raises:
            NotImplementedError: For broker types without a connector.
            TypeError: For unknown configuration types.
        """
        configs = list(config) if isinstance(config, (list, tuple)) else [config]
        return cls([create_connector(c) for c in configs], **kwargs)

    # ------------------------------------------------------------ properties

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has completed and :meth:`close` has not been called since."""
        return self._started

    @property
    def dropped(self) -> int:
        """Messages dropped because the dispatch queue was full."""
        return self._dropped

    @property
    def routes(self) -> list[Route]:
        """A copy of the registered routes, in registration order."""
        with self._lock:
            return list(self._routes)

    def status(self) -> list[dict[str, Any]]:
        """Connector rows from the bridge table (empty when the bridge is closed)."""
        return self._table.connectors()

    def routing_table(self) -> list[dict[str, Any]]:
        """Route rows from the bridge table (empty when the bridge is closed)."""
        return self._table.routes()

    # --------------------------------------------------------------- routing

    def add_route(self, route: Route) -> Route:
        """Register a route; subscribe at once if the Bridge is running.

        Adding the same route object twice has no effect.

        Args:
            route: The route to add.

        Returns:
            ``route``, for chaining.
        """
        with self._lock:
            if route in self._routes:
                return route
            self._routes.append(route)
            if self._started:
                self._table.add_route(route)
                self._sync_subscription(route.topic)
        return route

    def drop_route(self, route: Route) -> bool:
        """Remove a route. The broker subscription is dropped with the last route using it.

        If other routes still use the same topic filter, the subscription is
        kept but re-sent when the highest requested QoS changes.

        Args:
            route: The route to remove (compared by identity).

        Returns:
            ``True`` if the route was registered and has been removed.
        """
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
        """Highest explicit QoS among the routes on ``topic``, or ``None`` if none set one."""
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

    # ------------------------------------------------------------ components

    def attach(self, component: Component) -> "Bridge":
        """Host a component (a Tuner, a Runner...): register its routes and, once running, start it.

        Args:
            component: The component to host.

        Returns:
            The Bridge, for chaining.

        Raises:
            RuntimeError: If the component is already attached.
        """
        if any(a.component is component for a in self._attached):
            raise RuntimeError("This component is already attached.")
        attached = _Attached(component, [self.add_route(r) for r in component.routes()])
        self._attached.append(attached)
        if self._started:
            component.start(self)
            attached.started = True
        return self

    def detach(self, component: Component) -> None:
        """Remove a component: drop its routes and stop it if it was started.

        Detaching a component that is not attached does nothing.

        Args:
            component: The component to remove.
        """
        attached = next((a for a in self._attached if a.component is component), None)
        if attached is None:
            return
        for route in attached.routes:
            self.drop_route(route)
        if attached.started:
            component.stop()
        self._attached.remove(attached)

    # ------------------------------------------------------------- messaging

    def publish(self, message: Message, connector_id: str | None = None) -> None:
        """Publish a message through one connector.

        Args:
            message: The message to send.
            connector_id: Connector to use; ``None`` means the first one.

        Raises:
            RuntimeError: If the Bridge is not running.
            KeyError: If ``connector_id`` is unknown.
        """
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
        """Dispatch loop: take messages off the queue until the stop event is set.

        The 0.2 s ``get`` timeout bounds how long :meth:`close` waits for the
        loop to notice the stop event when the queue is idle.
        """
        q = self._queue
        while not self._stop.is_set():
            try:
                message = q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._dispatch(message)

    def _dispatch(self, message: Message) -> None:
        """Call every matching route's handler; log and continue if one raises.

        The matching routes are snapshotted under the lock, but handlers are
        called outside it so that a handler may add or drop routes.
        """
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
        """Connect, subscribe, and start the hosted components.

        Order: open the table, start the dispatch thread, connect each
        connector (recording ``registered`` / ``running`` / ``error`` in the
        table), subscribe to every route topic, mark the Bridge started, then
        start components in attach order. Components therefore find the
        Bridge ready to publish when ``start`` is called on them.

        Returns:
            The Bridge, for chaining.

        Raises:
            RuntimeError: If the Bridge is already running.
            Exception: Whatever a connector's ``connect`` or a component's
                ``start`` raises (for example ``OSError``). The Bridge is
                closed before the exception propagates.
        """
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

            for attached in self._attached:
                attached.component.start(self)
                attached.started = True
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Stop everything. Safe to call more than once, and after a failed start.

        Order: signal the dispatch thread, disconnect connectors, wait up to
        5 s for the dispatch thread (it may be inside a slow handler), stop
        started components in reverse attach order, then close the table.
        Errors from connectors and components are logged, not raised. When
        called from a handler (on the dispatch thread) it does not join
        itself.
        """
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

        for attached in reversed(self._attached):
            if attached.started:
                try:
                    attached.component.stop()
                except Exception:
                    log.exception("Error stopping %s.", type(attached.component).__name__)
                attached.started = False

        self._table.close()
        with self._lock:
            self._subscribed.clear()
            self._started = False
        self._queue = None
        self._thread = None

    def __enter__(self) -> "Bridge":
        """Start the Bridge and return it."""
        return self.start()

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> bool:
        """Close the Bridge; exceptions from the ``with`` body propagate."""
        self.close()
        return False
