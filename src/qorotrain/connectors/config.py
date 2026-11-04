from dataclasses import dataclass, field

from ..messages.message import Message


@dataclass(frozen=True)
class MQTTConfig:
    host: str = "localhost"
    port: int = 1883
    client_id: str = ""  # empty -> a unique ID is generated for each connector
    qos: int = 1  # default QoS for publish and subscribe
    keepalive: int = 60
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    # Last Will: published by the broker if this client disconnects unexpectedly.
    will: Message | None = None

    def __post_init__(self) -> None:
        if self.qos not in (0, 1, 2):
            raise ValueError("qos must be 0, 1 or 2.")
        if self.will is not None and self.will.qos not in (None, 0, 1, 2):
            raise ValueError("will.qos must be 0, 1 or 2.")


@dataclass(frozen=True)
class KafkaConfig:
    host: str = "localhost"


@dataclass(frozen=True)
class RabbitMQConfig:
    host: str = "localhost"


BrokerConfig = MQTTConfig | KafkaConfig | RabbitMQConfig