"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/connectors/config.py

Description:
    Runtime configuration objects for broker connectors.

    These frozen dataclasses are the *programmatic* configuration consumed by
    :func:`~qorotrain.connectors.factory.create_connector`. They are distinct
    from the user-facing YAML schema
    (:class:`~qorotrain.config.models.ConnectorSpec`), which converts itself
    into an :class:`MQTTConfig` with ``to_config()``. Keeping the two apart
    lets the messaging layer be used (and tested) without any YAML.

    :class:`KafkaConfig` and :class:`RabbitMQConfig` are placeholders that
    reserve the names in :data:`BrokerConfig`; no connector implements them.

===============================================================================
"""
from dataclasses import dataclass, field

from ..messages.message import Message


@dataclass(frozen=True)
class MQTTConfig:
    """Connection settings for :class:`~qorotrain.connectors.connector.MQTTConnector`.

    Attributes:
        host (str): Broker host name or IP address.
        port (int): Broker TCP port.
        client_id (str): MQTT client identifier. Empty means a unique
            ``qorotrain-<random>`` identifier is generated per connector.
        qos (int): Default QoS for publish and subscribe calls that do not
            specify one.
        keepalive (int): MQTT keep-alive interval, in seconds.
        username (str | None): Broker username, if authentication is used.
        password (str | None): Broker password. Excluded from ``repr`` so it
            does not leak into logs.
        will (Message | None): Last Will message, published by the broker if
            this client disconnects unexpectedly.

    Raises:
        ValueError: On construction, if ``qos`` or ``will.qos`` is not 0, 1
            or 2.
    """

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
        """Validate the QoS values.

        Raises:
            ValueError: If ``qos`` is not 0, 1 or 2, or ``will.qos`` is not
                ``None``, 0, 1 or 2.
        """
        if self.qos not in (0, 1, 2):
            raise ValueError("qos must be 0, 1 or 2.")
        if self.will is not None and self.will.qos not in (None, 0, 1, 2):
            raise ValueError("will.qos must be 0, 1 or 2.")


@dataclass(frozen=True)
class KafkaConfig:
    """Placeholder for a future Kafka connector (not implemented).

    Attributes:
        host (str): Broker host name.
    """

    host: str = "localhost"


@dataclass(frozen=True)
class RabbitMQConfig:
    """Placeholder for a future RabbitMQ connector (not implemented).

    Attributes:
        host (str): Broker host name.
    """

    host: str = "localhost"


BrokerConfig = MQTTConfig | KafkaConfig | RabbitMQConfig
"""Union of every broker configuration type the factory recognises."""
