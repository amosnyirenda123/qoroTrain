"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/helpers.py

Description:
    Shared test doubles for the messaging layer.

    ``FakeConnector`` is an in-memory
    :class:`~qorotrain.connectors.connector.Connector` that records subscribe,
    publish, flush and disconnect calls, can fail on connect or publish, can
    loop published messages back like a broker, and lets a test inject messages
    with ``deliver`` as if they arrived on the network thread. ``wait_for``
    polls a predicate, for assertions about work done on background threads.

===============================================================================
"""
import time

from qorotrain.connectors.connector import Connector
from qorotrain.messages.message import Message


class FakeConnector(Connector):
    """In-memory connector: records calls and lets a test deliver messages."""

    def __init__(self, fail_connect: bool = False, loopback: bool = False) -> None:
        self.fail_connect = fail_connect
        self.loopback = loopback  # deliver our own publishes back, as a broker does for subscribers
        self.flushed: list[float] = []
        self.flush_result = True
        self.publish_error: Exception | None = None
        self.calls: list[str] = []  # order of publish / flush / disconnect
        self.connected = False
        self.subscribed: list[tuple[str, int | None]] = []
        self.unsubscribed: list[str] = []
        self.published: list[Message] = []
        self._callback = None

    def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("broker unreachable")
        self.connected = True

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    def publish(self, msg: Message) -> None:
        if self.publish_error is not None:
            raise self.publish_error
        self.calls.append("publish")
        self.published.append(msg)
        if self.loopback and self._callback is not None:
            self._callback(msg)

    def flush(self, timeout: float = 5.0) -> bool:
        self.calls.append("flush")
        self.flushed.append(timeout)
        return self.flush_result

    def subscribe(self, topic: str, qos: int | None = None) -> None:
        self.subscribed.append((topic, qos))

    def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)

    def on_message(self, callback) -> None:
        self._callback = callback

    def deliver(self, topic: str, payload: bytes = b"{}") -> None:
        """Simulate the broker client's network thread delivering a message."""
        self._callback(Message(topic=topic, payload=payload))


def wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()