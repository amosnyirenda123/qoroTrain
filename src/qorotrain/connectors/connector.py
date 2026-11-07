"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/connectors/connector.py

Description:
    Broker abstraction (:class:`Connector`) and its MQTT implementation
    (:class:`MQTTConnector`, built on Eclipse Paho 2.x with MQTT v5).

    A connector owns one client connection to one broker. It publishes
    :class:`~qorotrain.messages.message.Message` objects, keeps a set of
    subscriptions, and hands *every* received message to a single callback.
    It does no routing; that is the Bridge's job. The Tracker also uses a
    connector directly, without a Bridge, to publish telemetry from inside the
    notebook.

    Threading:
        Paho runs its network loop on a background thread
        (``loop_start``). All ``_on_*`` callbacks, and therefore the message
        callback, run on that thread and must return quickly. Public methods
        may be called from any thread; the subscription registry is protected
        by a lock because ``_on_connect`` reads it on the network thread.

    Reliability:
        Subscriptions are remembered and replayed on every (re)connect, so
        they survive broker restarts handled by Paho's automatic reconnect.
        Publishes with QoS > 0 are tracked so that :meth:`MQTTConnector.flush`
        can wait for the broker's acknowledgements before disconnecting.

===============================================================================
"""
import logging
import threading
import time
import uuid
from collections import deque
from abc import ABC, abstractmethod
from typing import Any, Callable

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from .config import MQTTConfig
from ..messages.message import Message

log = logging.getLogger(__name__)

MessageCallback = Callable[[Message], None]
"""Signature of the callback that receives every incoming message."""


class Connector(ABC):
    """Broker abstraction.

    A connector delivers *every* incoming message to a single callback
    (see :meth:`on_message`). Routing is the Bridge's job, not the connector's.
    The callback runs on the broker client's network thread, so it must return
    quickly (enqueue the message and leave).

    Usable as a context manager: ``with connector:`` connects on entry and
    disconnects on exit.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the connection and start delivering messages.

        Raises:
            OSError: If the broker cannot be reached (implementation
                specific subclasses such as ``ConnectionRefusedError``).
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection. Must be safe to call when not connected."""
        ...

    @abstractmethod
    def publish(self, msg: Message) -> None:
        """Send a message; must not block waiting for the broker.

        Args:
            msg: The message. ``msg.qos=None`` selects the connector default.
        """
        ...

    @abstractmethod
    def subscribe(self, topic: str, qos: int | None = None) -> None:
        """Subscribe to a topic filter, now and after every reconnect.

        Args:
            topic: Topic filter (wildcards allowed).
            qos: Requested QoS, or ``None`` for the connector default.
        """
        ...

    @abstractmethod
    def unsubscribe(self, topic: str) -> None:
        """Remove a subscription previously made with :meth:`subscribe`.

        Args:
            topic: The exact filter that was subscribed.
        """
        ...

    @abstractmethod
    def on_message(self, callback: MessageCallback) -> None:
        """Register the callback that receives every incoming message.

        Args:
            callback: Called on the network thread; replaces any previous
                callback.
        """
        ...

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until published messages are acknowledged. True if all were (or nothing to wait for).

        The base implementation has nothing to wait for and returns ``True``.

        Args:
            timeout: Maximum total wait, in seconds.

        Returns:
            Whether every tracked message was acknowledged.
        """
        return True

    def __enter__(self) -> "Connector":
        """Connect and return the connector."""
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        """Disconnect."""
        self.disconnect()


class MQTTConnector(Connector):
    """MQTT v5 connector built on ``paho-mqtt`` 2.x.

    Headers of a :class:`~qorotrain.messages.message.Message` are carried as
    MQTT 5 user properties in both directions.

    Attributes:
        client_id (str): The MQTT client identifier in use (read-only).
    """

    def __init__(self, config: MQTTConfig) -> None:
        """Create the Paho client; no network activity happens yet.

        Credentials and the Last Will are configured on the client here,
        because Paho requires them before ``connect``.

        Args:
            config: Connection settings. An empty ``client_id`` is replaced by
                ``qorotrain-<12 hex digits>``.
        """
        self._cfg = config
        self._client_id = config.client_id or f"qorotrain-{uuid.uuid4().hex[:12]}"

        self._subs: dict[str, int] = {}
        self._subs_lock = threading.Lock()
        self._callback: MessageCallback | None = None
        self._inflight: deque[mqtt.MQTTMessageInfo] = deque(maxlen=10_000)  # publishes with qos > 0, awaiting ack

        self._connack = threading.Event()
        self._connect_error: str | None = None

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mqtt.MQTTv5,
        )
        if config.username is not None:
            self._client.username_pw_set(config.username, config.password)
        if config.will is not None:
            will = config.will
            self._client.will_set(
                will.topic,
                will.payload,
                qos=config.qos if will.qos is None else will.qos,
                retain=will.retain,
            )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ------------------------------------------------------------ lifecycle

    @property
    def client_id(self) -> str:
        """The MQTT client identifier used for this connection."""
        return self._client_id

    def connect(self, timeout: float = 5.0) -> None:
        """Connect, start the network thread, and wait for the broker's CONNACK.

        Args:
            timeout: Seconds to wait for the CONNACK.

        Raises:
            OSError: If the TCP connection fails (for example
                ``ConnectionRefusedError``).
            TimeoutError: If no CONNACK arrives within ``timeout``.
            ConnectionError: If the broker refuses the connection (bad
                credentials, client id rejected...).
        """
        self._connack.clear()
        self._connect_error = None

        # Raises OSError (e.g. ConnectionRefusedError) if the broker is unreachable.
        self._client.connect(self._cfg.host, self._cfg.port, self._cfg.keepalive)
        self._client.loop_start()

        if not self._connack.wait(timeout):
            self._client.loop_stop()
            raise TimeoutError("MQTT connection was not acknowledged")
        if self._connect_error is not None:
            self._client.loop_stop()
            raise ConnectionError(f"MQTT connection refused: {self._connect_error}")

    def disconnect(self) -> None:
        """Send DISCONNECT if connected and stop the network thread."""
        if self._client.is_connected():
            self._client.disconnect()
        self._client.loop_stop()
        self._connack.clear()

    # ------------------------------------------------------------ messaging

    def on_message(self, callback: MessageCallback) -> None:
        """Register the callback that receives every incoming message.

        Args:
            callback: Called on Paho's network thread. Exceptions it raises
                are logged and swallowed so they cannot kill the network loop.
        """
        self._callback = callback

    def subscribe(self, topic: str, qos: int | None = None) -> None:
        """Record a subscription and send it now if connected.

        Subscribing again to the same filter updates its QoS.

        Args:
            topic: Topic filter.
            qos: Requested QoS, or ``None`` for ``config.qos``.
        """
        qos = self._cfg.qos if qos is None else qos
        with self._subs_lock:
            self._subs[topic] = qos
        if self._client.is_connected():
            self._client.subscribe(topic, qos=qos)

    def unsubscribe(self, topic: str) -> None:
        """Forget a subscription and send UNSUBSCRIBE now if connected.

        Args:
            topic: The subscribed filter.
        """
        with self._subs_lock:
            self._subs.pop(topic, None)
        if self._client.is_connected():
            self._client.unsubscribe(topic)

    def publish(self, msg: Message) -> None:
        """Queue a message for sending (non-blocking).

        Messages with QoS > 0 are remembered (up to 10,000; older entries are
        discarded) so that :meth:`flush` can wait for their acknowledgement.

        Args:
            msg: The message to publish.
        """
        props = None
        if msg.headers:
            props = Properties(PacketTypes.PUBLISH)
            props.UserProperty = list(msg.headers.items())
        qos = self._cfg.qos if msg.qos is None else msg.qos
        info = self._client.publish(
            msg.topic, msg.payload, qos=qos, retain=msg.retain, properties=props
        )
        if qos > 0:
            self._inflight.append(info)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for the acknowledgement of every tracked QoS > 0 publish.

        The ``timeout`` is a shared deadline for all messages, not a
        per-message wait. Messages that Paho reports as failed (for example
        because the client is disconnected) count as not acknowledged.

        Args:
            timeout: Maximum total wait, in seconds.

        Returns:
            ``True`` if every tracked message was acknowledged in time.
        """
        deadline = time.monotonic() + timeout
        ok = True
        while self._inflight:
            info = self._inflight.popleft()
            try:
                info.wait_for_publish(timeout=max(0.0, deadline - time.monotonic()))
            except (RuntimeError, ValueError):
                ok = False
                continue
            if not info.is_published():
                ok = False
        return ok

    # ------------------------------------------------- paho callbacks (network thread)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        """Paho CONNACK callback: record failure, or replay subscriptions on success.

        Always sets the CONNACK event so that :meth:`connect` stops waiting.
        """
        if reason_code.is_failure:
            self._connect_error = str(reason_code)
            log.error("MQTT connect failed: %s", reason_code)
            self._connack.set()
            return

        # Copy under the lock: subscribe() may run on another thread meanwhile.
        with self._subs_lock:
            subs = list(self._subs.items())
        for topic, qos in subs:
            client.subscribe(topic, qos=qos)

        self._connect_error = None
        self._connack.set()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        """Paho disconnect callback: clear the CONNACK event and log unexpected disconnects."""
        self._connack.clear()
        if getattr(reason_code, "is_failure", False):
            log.warning("MQTT connection lost: %s", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, m: mqtt.MQTTMessage) -> None:
        """Paho message callback: convert to :class:`Message` and invoke the user callback.

        Messages arriving before a callback is registered are dropped.
        """
        callback = self._callback
        if callback is None:
            return
        props = getattr(getattr(m, "properties", None), "UserProperty", None) or []
        msg = Message(
            topic=m.topic,
            payload=bytes(m.payload),
            headers=dict(props),
            qos=m.qos,
            retain=bool(m.retain),
        )
        try:
            callback(msg)
        except Exception:
            log.exception("message callback failed for %s", m.topic)
