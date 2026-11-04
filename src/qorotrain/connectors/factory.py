from .config import BrokerConfig, KafkaConfig, MQTTConfig, RabbitMQConfig
from .connector import Connector, MQTTConnector


def create_connector(config: BrokerConfig) -> Connector:
    """Build the connector that matches a broker config."""
    match config:
        case MQTTConfig():
            return MQTTConnector(config)
        case RabbitMQConfig() | KafkaConfig():
            raise NotImplementedError(f"{type(config).__name__} is not supported yet.")
        case _:
            raise TypeError(f"Unsupported broker config: {type(config).__name__}")