"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/messages/message.py

Description:
    Broker-neutral message envelope shared by the whole messaging layer.

    Connectors translate between this type and their client library's native
    message (for MQTT, Paho's ``MQTTMessage``); routes, the Bridge and the
    components only ever see :class:`Message`. That keeps broker-specific code
    confined to the ``qorotrain.connectors`` package.

    The payload is opaque bytes. The JSON contract that Qorotrain's components
    put inside it is defined in :mod:`qorotrain.config.contracts.schema`.

===============================================================================
"""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Message:
    """A broker-neutral message.

    ``qos=None`` means "use the connector's default". ``retain`` is only
    meaningful for brokers that support retained messages (MQTT).

    Instances are immutable (``frozen=True``) because the same message object
    may be handed to several route handlers in turn.

    Attributes:
        topic (str): Concrete topic the message was published on (never a
            wildcard pattern).
        payload (bytes): Message body, uninterpreted at this layer.
        headers (Mapping[str, str]): Optional key/value metadata. MQTT maps it
            to MQTT 5 user properties.
        qos (int | None): Quality of service 0, 1 or 2, or ``None`` for the
            connector default. On received messages it is the delivered QoS.
        retain (bool): Ask the broker to keep the message for late
            subscribers (MQTT only).
    """

    topic: str
    payload: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    qos: int | None = None
    retain: bool = False
