"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_mqtt_connector.py

Description:
    Unit tests of :class:`~qorotrain.connectors.connector.MQTTConnector` with
    Paho's client replaced by a mock: client ids, credentials and Last Will,
    CONNACK success/refusal/timeout, subscription replay on reconnect, publish
    QoS, retain and headers as MQTT 5 user properties, conversion of incoming
    messages, and isolation of callback exceptions.

===============================================================================
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import qorotrain.connectors.connector as connector_module
from qorotrain.connectors.config import MQTTConfig
from qorotrain.connectors.connector import MQTTConnector
from qorotrain.messages.message import Message


class ReasonCode:
    """Stands in for paho's ReasonCode."""

    def __init__(self, failure: bool = False) -> None:
        self.is_failure = failure

    def __str__(self) -> str:
        return "Not authorized" if self.is_failure else "Success"


def make(config=None) -> MQTTConnector:
    conn = MQTTConnector(config or MQTTConfig())
    conn._client = MagicMock()
    conn._client.is_connected.return_value = False
    return conn


def ack(conn, failure=False):
    """Make loop_start() behave like a broker answering with a CONNACK."""
    conn._client.loop_start.side_effect = lambda: conn._on_connect(
        conn._client, None, None, ReasonCode(failure), None
    )


# ---------------------------------------------------------------- identity


def test_client_id_is_generated_when_empty_and_unique():
    a, b = MQTTConnector(MQTTConfig()), MQTTConnector(MQTTConfig())
    assert a.client_id.startswith("qorotrain-")
    assert a.client_id != b.client_id


def test_explicit_client_id_is_kept():
    assert MQTTConnector(MQTTConfig(client_id="abc")).client_id == "abc"


def test_invalid_qos_in_config_is_rejected():
    with pytest.raises(ValueError):
        MQTTConfig(qos=5)


def test_credentials_and_last_will_are_configured(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(connector_module.mqtt, "Client", MagicMock(return_value=fake_client))
    will = Message("tuning/p/r/status", b'{"state":"failed"}', qos=1, retain=True)

    MQTTConnector(MQTTConfig(username="u", password="p", will=will))

    fake_client.username_pw_set.assert_called_once_with("u", "p")
    fake_client.will_set.assert_called_once_with(
        "tuning/p/r/status", b'{"state":"failed"}', qos=1, retain=True
    )


# --------------------------------------------------------------- connecting


def test_connect_succeeds_when_connack_is_received():
    conn = make()
    ack(conn)
    conn.connect(timeout=1)
    conn._client.connect.assert_called_once_with("localhost", 1883, 60)


def test_connect_raises_connection_error_when_refused():
    conn = make()
    ack(conn, failure=True)
    with pytest.raises(ConnectionError, match="Not authorized"):
        conn.connect(timeout=1)
    conn._client.loop_stop.assert_called()


def test_connect_times_out_without_connack():
    conn = make()
    with pytest.raises(TimeoutError):
        conn.connect(timeout=0.05)
    conn._client.loop_stop.assert_called()


# ------------------------------------------------------------ subscriptions


def test_subscriptions_are_replayed_on_every_connect():
    conn = make()
    conn.subscribe("a/+", qos=2)
    conn.subscribe("b")  # None -> the config default (1)

    conn._on_connect(conn._client, None, None, ReasonCode(), None)

    replayed = {c.args[0]: c.kwargs["qos"] for c in conn._client.subscribe.call_args_list}
    assert replayed == {"a/+": 2, "b": 1}


def test_failed_connect_does_not_subscribe():
    conn = make()
    conn.subscribe("a")
    conn._on_connect(conn._client, None, None, ReasonCode(failure=True), None)
    conn._client.subscribe.assert_not_called()


def test_subscribe_while_connected_goes_straight_to_the_broker():
    conn = make()
    conn._client.is_connected.return_value = True
    conn.subscribe("a", qos=0)
    conn._client.subscribe.assert_called_once_with("a", qos=0)


def test_unsubscribe_forgets_the_topic_for_later_reconnects():
    conn = make()
    conn.subscribe("a")
    conn.unsubscribe("a")
    conn._on_connect(conn._client, None, None, ReasonCode(), None)
    conn._client.subscribe.assert_not_called()


# --------------------------------------------------------------- publishing


def test_publish_defaults_to_config_qos_and_allows_override_and_retain():
    conn = make(MQTTConfig(qos=1))

    conn.publish(Message("t", b"x"))
    kwargs = conn._client.publish.call_args.kwargs
    assert kwargs["qos"] == 1 and kwargs["retain"] is False

    conn.publish(Message("t", b"x", qos=0, retain=True))
    kwargs = conn._client.publish.call_args.kwargs
    assert kwargs["qos"] == 0 and kwargs["retain"] is True


def test_publish_sends_headers_as_user_properties():
    conn = make()
    conn.publish(Message("t", b"x", headers={"k": "v"}))
    assert conn._client.publish.call_args.kwargs["properties"].UserProperty == [("k", "v")]

    conn.publish(Message("t", b"x"))
    assert conn._client.publish.call_args.kwargs["properties"] is None


# ---------------------------------------------------------------- receiving


def paho_message(**overrides):
    fields = dict(
        topic="t/1",
        payload=b"data",
        qos=1,
        retain=True,
        properties=SimpleNamespace(UserProperty=[("k", "v")]),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_incoming_message_is_converted_and_delivered():
    conn, got = make(), []
    conn.on_message(got.append)
    conn._on_message(conn._client, None, paho_message())
    assert got == [Message("t/1", b"data", {"k": "v"}, qos=1, retain=True)]


def test_message_without_properties_gets_empty_headers():
    conn, got = make(), []
    conn.on_message(got.append)
    conn._on_message(conn._client, None, paho_message(properties=None))
    assert got[0].headers == {}


def test_callback_exception_is_swallowed():
    conn = make()

    def bad(message):
        raise RuntimeError("boom")

    conn.on_message(bad)
    conn._on_message(conn._client, None, paho_message())  # must not raise


def test_message_without_callback_is_ignored():
    conn = make()
    conn._on_message(conn._client, None, paho_message())  # must not raise