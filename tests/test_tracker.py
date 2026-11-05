import math
import warnings

import pytest

import qorotrain.trackers.tracker as tracker_module
from helpers import FakeConnector
from qorotrain import Tracker
from qorotrain.config.environment import ENV_BROKER, ENV_PROJECT, ENV_RUN_ID, ENV_TOPIC_ROOT
from qorotrain.config.contracts.schema import decode_metrics


def make(fake=None, **kwargs):
    fake = fake or FakeConnector()
    kwargs.setdefault("flush_every", 1000)
    kwargs.setdefault("flush_seconds", 1000)
    return Tracker(run="r1", project="p", connector=fake, **kwargs), fake


def batches(fake):
    return [decode_metrics(m.payload) for m in fake.published]


def points(fake):
    return [(p.step, p.values) for b in batches(fake) for p in b.points]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (ENV_RUN_ID, ENV_PROJECT, ENV_BROKER, ENV_TOPIC_ROOT):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------- when disabled


def test_without_the_environment_the_tracker_does_nothing():
    tracker = Tracker()
    assert not tracker.enabled
    tracker.log(1, reward=1.0)  # no error, no network
    tracker.finish()


def test_a_run_id_alone_is_not_enough_to_enable_telemetry():
    assert not Tracker(run="r1").enabled
    assert not Tracker(project="p").enabled


def test_connection_failure_disables_telemetry_with_a_warning():
    with pytest.warns(RuntimeWarning, match="cannot connect to the broker"):
        tracker = Tracker(run="r1", project="p", connector=FakeConnector(fail_connect=True))
    assert not tracker.enabled
    tracker.log(1, reward=1.0)  # still harmless
    tracker.finish()


# ----------------------------------------------------------------- configuration


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_RUN_ID, "r42")
    monkeypatch.setenv(ENV_PROJECT, "demo")
    monkeypatch.setenv(ENV_TOPIC_ROOT, "lab/tuning")
    fake = FakeConnector()
    tracker = Tracker(connector=fake, flush_every=1)
    tracker.log(0, reward=1.0)
    message = fake.published[0]
    assert message.topic == "lab/tuning/demo/r42/metrics"
    assert decode_metrics(message.payload).run == "r42"


def test_explicit_arguments_beat_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_RUN_ID, "from-env")
    monkeypatch.setenv(ENV_PROJECT, "from-env")
    fake = FakeConnector()
    Tracker(run="explicit", project="proj", connector=fake, flush_every=1).log(0, a=1)
    assert fake.published[0].topic == "tuning/proj/explicit/metrics"


def test_messages_use_qos_1():
    tracker, fake = make(flush_every=1)
    tracker.log(0, reward=1.0)
    assert fake.published[0].qos == 1


# ---------------------------------------------------------------------- batching


def test_points_are_batched_by_size():
    tracker, fake = make(flush_every=3)
    for step in range(7):
        tracker.log(step, reward=float(step))
    assert [len(b.points) for b in batches(fake)] == [3, 3]  # one point still buffered

    tracker.finish()
    assert [len(b.points) for b in batches(fake)] == [3, 3, 1]
    assert [step for step, _ in points(fake)] == list(range(7))


def test_points_are_flushed_when_the_time_limit_has_passed():
    tracker, fake = make(flush_every=1000, flush_seconds=0)
    tracker.log(0, reward=1.0)
    tracker.log(1, reward=2.0)
    assert [len(b.points) for b in batches(fake)] == [1, 1]


def test_nothing_is_sent_before_a_limit_is_reached():
    tracker, fake = make()
    tracker.log(0, reward=1.0)
    assert fake.published == []


# ------------------------------------------------------------------------ finish


def test_finish_sends_the_rest_waits_for_the_broker_and_disconnects():
    tracker, fake = make()
    tracker.log(0, reward=1.0)
    tracker.finish()
    assert points(fake) == [(0, {"reward": 1.0})]
    assert len(fake.flushed) == 1  # waited for acknowledgements
    assert fake.connected is False


def test_finish_flushes_before_it_disconnects():
    """Disconnecting first could drop the last batch; the order is part of the contract."""
    tracker, fake = make()
    tracker.log(0, reward=1.0)
    fake.calls.clear()
    tracker.finish()
    assert fake.calls == ["publish", "flush", "disconnect"]


def test_finish_is_idempotent_and_stops_logging():
    tracker, fake = make()
    tracker.log(0, reward=1.0)
    tracker.finish()
    tracker.finish()
    tracker.log(1, reward=2.0)  # ignored after finish
    assert len(fake.published) == 1 and len(fake.flushed) == 1
    assert not tracker.enabled


def test_finish_with_nothing_buffered_sends_nothing():
    tracker, fake = make()
    tracker.finish()
    assert fake.published == []


def test_unacknowledged_messages_produce_a_warning():
    tracker, fake = make()
    fake.flush_result = False
    tracker.log(0, reward=1.0)
    with pytest.warns(RuntimeWarning, match="did not acknowledge"):
        tracker.finish()
    assert fake.connected is False  # still disconnects


def test_context_manager_finishes_even_when_the_body_raises():
    fake = FakeConnector()
    with pytest.raises(RuntimeError):
        with Tracker(run="r1", project="p", connector=fake, flush_every=1000) as tracker:
            tracker.log(0, reward=1.0)
            raise RuntimeError("training blew up")
    assert points(fake) == [(0, {"reward": 1.0})]


def test_finish_is_registered_for_interpreter_exit_and_unregistered_afterwards(monkeypatch):
    registered, unregistered = [], []
    monkeypatch.setattr(tracker_module.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(tracker_module.atexit, "unregister", lambda fn: unregistered.append(fn))
    tracker, _ = make()
    assert registered == [tracker.finish]
    tracker.finish()
    assert unregistered == [tracker.finish]


# ------------------------------------------------------------------------ values


def test_step_can_be_positional_or_keyword():
    tracker, fake = make()
    tracker.log(1, reward=1.0)
    tracker.log(step=2, reward=2.0)
    tracker.finish()
    assert [step for step, _ in points(fake)] == [1, 2]


def test_number_like_values_are_converted():
    class Scalar:  # stands in for numpy scalars and 0-d tensors
        def __float__(self):
            return 2.5

    tracker, fake = make()
    tracker.log(0, loss=Scalar(), count=3, flag=True)
    tracker.finish()
    assert points(fake) == [(0, {"loss": 2.5, "count": 3.0, "flag": 1.0})]


def test_non_numeric_values_are_dropped_with_one_warning_but_the_rest_is_kept():
    tracker, fake = make()
    with pytest.warns(RuntimeWarning, match="metric 'label' is not a number"):
        tracker.log(0, reward=1.0, label="easy")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a second warning would raise
        tracker.log(1, reward=2.0, label="hard")
    tracker.finish()
    assert points(fake) == [(0, {"reward": 1.0}), (1, {"reward": 2.0})]


def test_a_point_with_no_usable_values_is_dropped():
    tracker, fake = make()
    with pytest.warns(RuntimeWarning):
        tracker.log(0, label="x")
    tracker.finish()
    assert fake.published == []


def test_a_bad_step_drops_the_point_with_a_warning():
    tracker, fake = make()
    with pytest.warns(RuntimeWarning, match="step must be an integer"):
        tracker.log("later", reward=1.0)
    tracker.finish()
    assert fake.published == []


def test_nan_is_kept_so_divergence_can_be_detected():
    tracker, fake = make()
    tracker.log(0, loss=float("nan"))
    tracker.finish()
    assert math.isnan(points(fake)[0][1]["loss"])


# ---------------------------------------------------------------------- failures


def test_a_publish_error_never_reaches_the_training_code():
    tracker, fake = make(flush_every=1)
    fake.publish_error = ConnectionError("broker went away")
    with pytest.warns(RuntimeWarning, match="could not publish telemetry"):
        tracker.log(0, reward=1.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # warned once only
        tracker.log(1, reward=2.0)
    tracker.finish()
    assert tracker.enabled is False


def test_a_flush_error_never_reaches_the_training_code():
    tracker, fake = make()

    def boom(timeout=5.0):
        raise OSError("socket closed")

    fake.flush = boom
    tracker.log(0, reward=1.0)
    with pytest.warns(RuntimeWarning, match="could not flush"):
        tracker.finish()
    assert fake.connected is False