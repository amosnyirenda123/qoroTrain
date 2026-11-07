"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/connectors/factory.py

Description:
    Factory that turns a broker configuration into a connector instance.

    The Bridge accepts configuration objects
    (:meth:`~qorotrain.bridges.bridge.Bridge.from_config`) and uses this
    function to build the matching
    :class:`~qorotrain.connectors.connector.Connector`. Dispatching on the
    configuration *type* (structural pattern matching) keeps the set of
    supported brokers in one place: adding a broker means adding a config
    dataclass, a connector class and one ``case`` here.

===============================================================================
"""
from .config import BrokerConfig, KafkaConfig, MQTTConfig, RabbitMQConfig
from .connector import Connector, MQTTConnector


def create_connector(config: BrokerConfig) -> Connector:
    """Build the connector that matches a broker config.

    The connector is created but not connected; the Bridge connects it when
    it starts.

    Args:
        config: Broker configuration. Its type selects the connector class.

    Returns:
        A new, unconnected connector for ``config``.

    Raises:
        NotImplementedError: For
            :class:`~qorotrain.connectors.config.RabbitMQConfig` and
            :class:`~qorotrain.connectors.config.KafkaConfig`, which are declared but not implemented.
        TypeError: If ``config`` is not a known broker configuration type.
    """
    match config:
        case MQTTConfig():
            return MQTTConnector(config)
        case RabbitMQConfig() | KafkaConfig():
            raise NotImplementedError(f"{type(config).__name__} is not supported yet.")
        case _:
            raise TypeError(f"Unsupported broker config: {type(config).__name__}")
