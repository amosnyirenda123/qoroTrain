from dataclasses import dataclass, field


@dataclass(frozen=True)
class MQTTConfig:
    host: str = "localhost"
    port: int = 1883
    client_id: str = ""
    qos: int = 1


@dataclass(frozen=True)
class KafkaConfig:
    host: str = "localhost"

@dataclass(frozen=True)
class RabbitMQConfig:
    host: str = "localhost"


BrokerConfig = MQTTConfig | KafkaConfig | RabbitMQConfig