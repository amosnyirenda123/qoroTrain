"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: tests/test_bridge.py

Description:
    Unit tests of :class:`~qorotrain.bridges.bridge.Bridge` and
    :class:`~qorotrain.bridges.table.BridgeTable` with fake connectors: routing
    and wildcard matching, dispatch on a single thread in arrival order, handler
    isolation, queue overflow accounting, start/close/restart lifecycle,
    subscription bookkeeping (QoS upgrades, unsubscribe with the last route),
    publishing, the SQLite mirror, component attach/detach and ``from_config``.

    Note: ``test_cli.py`` currently contains a newer copy of these tests with
    extra component-lifecycle cases.

===============================================================================
"""
import threading

import pytest

from helpers import FakeConnector, wait_for
from qorotrain.bridges.bridge import Bridge
from qorotrain.bridges.table import BridgeTable
from qorotrain.connectors.config import MQTTConfig, RabbitMQConfig
from qorotrain.connectors.connector import MQTTConnector
from qorotrain.messages.message import Message
from qorotrain.routes.route import Route
from qorotrain.tuners.tuner import Proposal, Tuner


def make_bridge(*connectors, routes=None, **kwargs):
    kwargs.setdefault("db_path", ":memory:")
    return Bridge(list(connectors), routes=routes, **kwargs)


def noop(message):
    pass


# --------------------------------------------------------------- dispatching


def test_two_routes_on_one_topic_both_receive_and_subscribe_once():
    c, a, b = FakeConnector(), [], []
    routes = [Route("t/x", a.append), Route("t/x", b.append)]
    with make_bridge(c, routes=routes):
        c.deliver("t/x")
        assert wait_for(lambda: len(a) == 1 and len(b) == 1)
    assert c.subscribed == [("t/x", None)]


def test_handlers_run_on_the_dispatch_thread_not_the_delivering_thread():
    c, seen = FakeConnector(), []
    route = Route("t", lambda m: seen.append(threading.current_thread().name))
    with make_bridge(c, routes=[route]):
        c.deliver("t")
        assert wait_for(lambda: seen)
    assert seen == ["bridge-dispatch"]


def test_failing_handler_does_not_stop_dispatching():
    c, got = FakeConnector(), []

    def bad(message):
        raise RuntimeError("boom")

    routes = [Route("t", bad), Route("t", got.append)]
    with make_bridge(c, routes=routes):
        c.deliver("t")
        c.deliver("t")
        assert wait_for(lambda: len(got) == 2)


def test_wildcard_routing_and_unmatched_topics():
    c, got = FakeConnector(), []
    with make_bridge(c, routes=[Route("tuning/+/+/metrics", got.append)]):
        c.deliver("tuning/p/r/metrics")
        c.deliver("tuning/p/r/status")  # no route: ignored, no error
        c.deliver("tuning/p/r/metrics")
        assert wait_for(lambda: len(got) == 2)
    assert [m.topic for m in got] == ["tuning/p/r/metrics"] * 2


def test_messages_are_dispatched_in_arrival_order():
    c, got = FakeConnector(), []
    with make_bridge(c, routes=[Route("t", lambda m: got.append(m.payload))]):
        for i in range(50):
            c.deliver("t", str(i).encode())
        assert wait_for(lambda: len(got) == 50)
    assert got == [str(i).encode() for i in range(50)]


def test_full_queue_drops_messages_and_counts_them():
    c = FakeConnector()
    entered, release = threading.Event(), threading.Event()

    def slow(message):
        entered.set()
        release.wait(timeout=5)

    bridge = make_bridge(c, routes=[Route("t", slow)], queue_size=1)
    with bridge:
        c.deliver("t")  # taken by the dispatch thread, which then blocks
        assert entered.wait(timeout=2)
        c.deliver("t")  # fills the queue
        c.deliver("t")  # dropped
        assert bridge.dropped == 1
        release.set()


# ----------------------------------------------------------------- lifecycle


def test_start_failure_raises_and_cleans_up():
    good, bad = FakeConnector(), FakeConnector(fail_connect=True)
    bridge = make_bridge(good, bad)
    with pytest.raises(ConnectionError):
        bridge.start()
    assert not bridge.started
    assert good.connected is False
    assert not any(t.name == "bridge-dispatch" for t in threading.enumerate())


def test_bridge_can_restart_after_close():
    c, got = FakeConnector(), []
    bridge = make_bridge(c, routes=[Route("t", got.append)])
    bridge.start()
    bridge.close()
    bridge.start()
    c.deliver("t")
    assert wait_for(lambda: len(got) == 1)
    bridge.close()
    assert c.subscribed == [("t", None), ("t", None)]


def test_start_twice_raises_and_close_is_idempotent():
    bridge = make_bridge(FakeConnector())
    bridge.start()
    with pytest.raises(RuntimeError):
        bridge.start()
    bridge.close()
    bridge.close()


def test_context_manager_connects_and_disconnects():
    c = FakeConnector()
    with make_bridge(c) as bridge:
        assert bridge.started and c.connected
    assert not bridge.started and not c.connected


# -------------------------------------------------------- dynamic subscriptions


def test_add_route_after_start_subscribes_only_new_topics():
    c = FakeConnector()
    with make_bridge(c, routes=[Route("a", noop)]) as bridge:
        bridge.add_route(Route("a", noop))
        bridge.add_route(Route("b", noop))
    assert c.subscribed == [("a", None), ("b", None)]


def test_higher_qos_on_an_existing_topic_resubscribes():
    c = FakeConnector()
    with make_bridge(c, routes=[Route("t", noop, qos=0)]) as bridge:
        bridge.add_route(Route("t", noop, qos=2))
    assert c.subscribed == [("t", 0), ("t", 2)]


def test_drop_route_unsubscribes_only_when_the_last_route_is_gone():
    c = FakeConnector()
    r1, r2 = Route("t/x", noop), Route("t/x", noop)
    with make_bridge(c, routes=[r1]) as bridge:
        bridge.add_route(r2)
        assert c.subscribed == [("t/x", None)]
        assert bridge.drop_route(r1) is True
        assert c.unsubscribed == []
        assert bridge.drop_route(r2) is True
        assert c.unsubscribed == ["t/x"]
        assert bridge.drop_route(r2) is False


def test_dropped_route_no_longer_receives():
    c, got = FakeConnector(), []
    route = Route("t", got.append)
    with make_bridge(c, routes=[route]) as bridge:
        bridge.drop_route(route)
        c.deliver("t")
        c.deliver("t")
        assert not wait_for(lambda: got, timeout=0.3)


# ------------------------------------------------------------------- publish


def test_publish_uses_first_connector_by_default_or_the_named_one():
    first, second = FakeConnector(), FakeConnector()
    bridge = Bridge({"one": first, "two": second}, db_path=":memory:")
    msg = Message("t", b"x")
    with bridge:
        bridge.publish(msg)
        bridge.publish(msg, connector_id="two")
        with pytest.raises(KeyError):
            bridge.publish(msg, connector_id="nope")
    assert first.published == [msg]
    assert second.published == [msg]


def test_publish_requires_a_running_bridge():
    with pytest.raises(RuntimeError):
        make_bridge(FakeConnector()).publish(Message("t", b"x"))


# ----------------------------------------------------------- bridge table


def test_table_mirrors_connectors_and_routes():
    c = FakeConnector()
    route = Route("t/x", noop)
    with make_bridge(c, routes=[route]) as bridge:
        assert [r["status"] for r in bridge.status()] == ["running"]
        assert [r["topic"] for r in bridge.routing_table()] == ["t/x"]
        bridge.drop_route(route)
        assert bridge.routing_table() == []


def test_stale_connector_rows_are_reset_on_start(tmp_path):
    path = str(tmp_path / "bridge.db")
    old = BridgeTable(path)
    old.open()
    old.set_connector("ghost", "running", "MQTTConnector")  # left by a crashed process
    old.close()

    with Bridge([FakeConnector()], db_path=path) as bridge:
        rows = {r["connector_id"]: r["status"] for r in bridge.status()}
    assert rows == {"ghost": "stopped", "connector-0": "running"}


# --------------------------------------------------------------------- tuner


class DummyTuner(Tuner):
    def __init__(self):
        super().__init__()
        self.received, self.stopped, self.bridge_seen = [], False, None

    def start(self, bridge):
        super().start(bridge)
        self.bridge_seen = bridge

    def stop(self):
        super().stop()
        self.stopped = True

    def routes(self):
        return [Route("tuning/+/+/status", self.received.append)]

    def propose(self, history):
        return Proposal(params={})


def test_attached_tuner_gets_routes_start_and_stop():
    c, tuner = FakeConnector(), DummyTuner()
    bridge = make_bridge(c).attach(tuner)
    with bridge:
        assert tuner.bridge_seen is bridge
        c.deliver("tuning/p/r/status")
        assert wait_for(lambda: len(tuner.received) == 1)
    assert tuner.stopped


def test_second_attach_raises_and_detach_removes_routes():
    c, tuner = FakeConnector(), DummyTuner()
    bridge = make_bridge(c).attach(tuner)
    with pytest.raises(RuntimeError):
        bridge.attach(DummyTuner())
    bridge.detach()
    assert bridge.routes == []


# --------------------------------------------------------------- from_config


def test_from_config_builds_an_mqtt_connector_without_connecting():
    bridge = Bridge.from_config(MQTTConfig(), db_path=":memory:")
    assert isinstance(next(iter(bridge._connectors.values())), MQTTConnector)


def test_from_config_rejects_unimplemented_brokers():
    with pytest.raises(NotImplementedError):
        Bridge.from_config(RabbitMQConfig())