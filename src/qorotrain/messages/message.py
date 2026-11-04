from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Message:
    """A broker-neutral message.

    ``qos=None`` means "use the connector's default". ``retain`` is only
    meaningful for brokers that support retained messages (MQTT).
    """

    topic: str
    payload: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    qos: int | None = None
    retain: bool = False