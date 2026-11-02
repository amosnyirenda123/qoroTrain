import logging
import threading
from abc import ABC, abstractmethod
from typing import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from ..config import MQTTConfig
from ..messages.message import Message

log = logging.getLogger(__name__)
Handler = Callable[[Message], None]


class Connector(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def publish(self, msg: Message) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, handler: Handler) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


class MQTTConnector(Connector):
    def __init__(self, config: MQTTConfig) -> None:
        self._cfg = config
        self._subs: set[str] = set()
        self._connected = threading.Event()
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    
    def connect(self, timeout: float = 5.0) -> None:
        self._client.connect(self._cfg.host, self._cfg.port)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self._client.loop_stop()
            raise TimeoutError("MQTT connection was not acknowledged")

    def disconnect(self) -> None:
        if self._client.is_connected():
            self._client.disconnect()
        self._client.loop_stop()
        self._connected.clear()

    
    def subscribe(self, topic: str, handler: Handler) -> None:
        self._client.message_callback_add(topic, self._wrap(handler))
        self._subs.add(topic)
        if self._connected.is_set():
            self._client.subscribe(topic, qos=self._cfg.qos)

    def publish(self, msg: Message) -> None:
        props = Properties(PacketTypes.PUBLISH)
        if msg.headers:
            props.UserProperty = list(msg.headers.items())
        self._client.publish(
            msg.topic, msg.payload, qos=self._cfg.qos, properties=props
        )

    
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        for topic in self._subs:  
            client.subscribe(topic, qos=self._cfg.qos)
        self._connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected.clear()

    def _wrap(self, handler: Handler):
        def callback(client, userdata, m: mqtt.MQTTMessage):
            props = getattr(m.properties, "UserProperty", [])
            msg = Message(topic=m.topic, payload=m.payload, headers=dict(props))
            try:
                handler(msg)
            except Exception:
                log.exception("handler failed for %s", m.topic)
        return callback