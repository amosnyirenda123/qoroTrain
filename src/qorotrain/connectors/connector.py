import logging
import threading
import time
import uuid
from collections import deque
from abc import ABC, abstractmethod
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .config import MQTTConfig
from ..messages.message import Message

log = logging.getLogger(__name__)

MessageCallback = Callable[[Message], None]


class Connector(ABC):
    """Broker abstraction.

    A connector delivers *every* incoming message to a single callback
    (see :meth:`on_message`). Routing is the Bridge's job, not the connector's.
    The callback runs on the broker client's network thread, so it must return
    quickly (enqueue the message and leave).
    """

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def publish(self, msg: Message) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, qos: int | None = None) -> None: ...

    @abstractmethod
    def unsubscribe(self, topic: str) -> None: ...

    @abstractmethod
    def on_message(self, callback: MessageCallback) -> None: ...

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until published messages are acknowledged. True if all were (or nothing to wait for)."""
        return True

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


class MQTTConnector(Connector):
    def __init__(self, config: MQTTConfig) -> None:
        self._cfg = config
        self._client_id = config.client_id or f"qorotrain-{uuid.uuid4().hex[:12]}"

        self._subs: dict[str, int] = {}
        self._subs_lock = threading.Lock()
        self._callback: MessageCallback | None = None
        self._inflight: deque = deque(maxlen=10_000)  # publishes with qos > 0, awaiting ack

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
        return self._client_id

    def connect(self, timeout: float = 5.0) -> None:
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
        if self._client.is_connected():
            self._client.disconnect()
        self._client.loop_stop()
        self._connack.clear()

    # ------------------------------------------------------------ messaging

    def on_message(self, callback: MessageCallback) -> None:
        self._callback = callback

    def subscribe(self, topic: str, qos: int | None = None) -> None:
        qos = self._cfg.qos if qos is None else qos
        with self._subs_lock:
            self._subs[topic] = qos
        if self._client.is_connected():
            self._client.subscribe(topic, qos=qos)

    def unsubscribe(self, topic: str) -> None:
        with self._subs_lock:
            self._subs.pop(topic, None)
        if self._client.is_connected():
            self._client.unsubscribe(topic)

    def publish(self, msg: Message) -> None:
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

    def _on_connect(self, client, userdata, flags, reason_code, properties):
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

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connack.clear()
        if getattr(reason_code, "is_failure", False):
            log.warning("MQTT connection lost: %s", reason_code)

    def _on_message(self, client, userdata, m):
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