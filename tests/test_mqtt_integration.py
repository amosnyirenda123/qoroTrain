"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_mqtt_integration.py

Description:
    Integration tests against a real broker (Mosquitto on localhost:1883).

    Start one with ``docker compose up -d`` and run ``pytest -m broker``. The
    tests are skipped automatically when no broker is reachable.

===============================================================================
"""
import queue
import socket
import time
import uuid

import pytest

from helpers import wait_for
from qorotrain.bridges.bridge import Bridge
from qorotrain.connectors.config import MQTTConfig
from qorotrain.connectors.connector import MQTTConnector
from qorotrain.messages.message import Message
from qorotrain.routes.route import Route

pytestmark = pytest.mark.broker

HOST, PORT = "localhost", 1883


@pytest.fixture(scope="module", autouse=True)
def require_broker():
    try:
        socket.create_connection((HOST, PORT), timeout=0.5).close()
    except OSError:
        pytest.skip(f"No MQTT broker reachable on {HOST}:{PORT}")


@pytest.fixture
def prefix():
    return f"qorotrain-test/{uuid.uuid4().hex}"


def settle():
    """Give the broker a moment to process a SUBSCRIBE before publishing."""
    time.sleep(0.3)


def test_round_trip_preserves_payload_headers_and_qos(prefix):
    topic = f"{prefix}/metrics"
    received = queue.Queue()
    with MQTTConnector(MQTTConfig()) as sub, MQTTConnector(MQTTConfig()) as pub:
        sub.on_message(received.put)
        sub.subscribe(topic, qos=1)
        settle()
        pub.publish(Message(topic, b'{"step": 1}', headers={"run": "r1"}, qos=1))
        msg = received.get(timeout=3)
    assert msg.topic == topic
    assert msg.payload == b'{"step": 1}'
    assert msg.headers == {"run": "r1"}
    assert msg.qos == 1


def test_retained_message_reaches_a_late_subscriber(prefix):
    topic = f"{prefix}/status"
    received = queue.Queue()
    with MQTTConnector(MQTTConfig()) as pub:
        pub.publish(Message(topic, b'{"state":"running"}', qos=1, retain=True))
        settle()
        with MQTTConnector(MQTTConfig()) as late:
            late.on_message(received.put)
            late.subscribe(topic)
            msg = received.get(timeout=3)
        pub.publish(Message(topic, b"", qos=1, retain=True))  # clear the retained message
        settle()
    assert msg.payload == b'{"state":"running"}'
    assert msg.retain is True


def test_connect_to_a_closed_port_raises():
    with pytest.raises(OSError):
        MQTTConnector(MQTTConfig(port=1)).connect()


def test_bridge_routes_real_messages_to_every_matching_route(prefix):
    a, b = [], []
    routes = [
        Route(f"{prefix}/+/metrics", a.append),
        Route(f"{prefix}/+/metrics", b.append),  # same topic: both must receive
    ]
    with Bridge.from_config(MQTTConfig(), routes=routes, db_path=":memory:"):
        settle()
        with MQTTConnector(MQTTConfig()) as pub:
            pub.publish(Message(f"{prefix}/run1/metrics", b"1", qos=1))
        assert wait_for(lambda: len(a) == 1 and len(b) == 1, timeout=3)


def test_bridge_publish_reaches_a_separate_subscriber(prefix):
    topic = f"{prefix}/command/start"
    received = queue.Queue()
    with MQTTConnector(MQTTConfig()) as sub:
        sub.on_message(received.put)
        sub.subscribe(topic, qos=1)
        settle()
        with Bridge.from_config(MQTTConfig(), db_path=":memory:") as bridge:
            bridge.publish(Message(topic, b'{"learning_rate": 0.001}', qos=1))
            msg = received.get(timeout=3)
    assert msg.payload == b'{"learning_rate": 0.001}'


def test_dropping_the_last_route_stops_delivery(prefix):
    got = []
    route = Route(f"{prefix}/t", got.append)
    with Bridge.from_config(MQTTConfig(), routes=[route], db_path=":memory:") as bridge:
        settle()
        with MQTTConnector(MQTTConfig()) as pub:
            pub.publish(Message(f"{prefix}/t", b"1", qos=1))
            assert wait_for(lambda: len(got) == 1, timeout=3)
            bridge.drop_route(route)
            settle()
            pub.publish(Message(f"{prefix}/t", b"2", qos=1))
            assert not wait_for(lambda: len(got) > 1, timeout=0.7)