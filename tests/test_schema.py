import json
import math

import pytest

from qorotrain.config.contracts.schema import (
    MetricBatch,
    MetricPoint,
    SchemaError,
    StartCommand,
    Status,
    StopCommand,
    decode_metrics,
    decode_start,
    decode_status,
    decode_stop,
    encode,
    metrics_pattern,
    metrics_topic,
    run_from_topic,
    start_topic,
    status_pattern,
    status_topic,
    stop_topic,
)
from qorotrain.routes.route import topic_matches


def raw(**fields) -> bytes:
    return json.dumps({"v": 1, **fields}).encode()


# ------------------------------------------------------------------- round trips


def test_start_command_round_trip():
    cmd = StartCommand("r7", {"learning_rate": 3e-4, "hidden_size": 64, "tag": "x"})
    assert decode_start(encode(cmd)) == cmd


def test_start_command_without_params_defaults_to_empty():
    assert decode_start(raw(run="r1")) == StartCommand("r1", {})


def test_stop_command_round_trip():
    assert decode_stop(encode(StopCommand("r7"))) == StopCommand("r7")


def test_status_round_trip_with_and_without_details():
    plain = Status("r1", "started", ts=10.0)
    assert decode_status(encode(plain)) == plain
    failed = Status("r1", "failed", exit_code=1, error="RuntimeError: boom", ts=11.0)
    assert decode_status(encode(failed)) == failed


def test_metric_batch_round_trip():
    batch = MetricBatch("r1", [MetricPoint(0, {"reward": 1.5, "loss": 0.25}, t=1.0), MetricPoint(1, {"reward": 2.0}, t=2.0)])
    assert decode_metrics(encode(batch)) == batch


def test_nan_and_infinity_survive_because_divergence_detection_needs_them():
    batch = decode_metrics(encode(MetricBatch("r1", [MetricPoint(3, {"loss": float("nan"), "norm": float("inf")})])))
    values = batch.points[0].values
    assert math.isnan(values["loss"]) and math.isinf(values["norm"])


def test_integers_in_metric_values_become_floats():
    batch = decode_metrics(raw(run="r1", points=[{"step": 0, "values": {"n": 3}}]))
    assert batch.points[0].values == {"n": 3.0} and isinstance(batch.points[0].values["n"], float)


def test_every_payload_carries_the_schema_version():
    for message in (StartCommand("r"), StopCommand("r"), Status("r", "started"), MetricBatch("r", [])):
        assert json.loads(encode(message))["v"] == 1


def test_encoding_an_unknown_type_is_a_type_error():
    with pytest.raises(TypeError):
        encode("not a message")


def test_terminal_flag():
    assert not Status("r", "started").terminal
    assert all(Status("r", s).terminal for s in ("finished", "failed", "stopped"))


# ------------------------------------------------------------------- strictness


@pytest.mark.parametrize(
    "payload, fragment",
    [
        (b"nope", "not valid JSON"),
        (b"\xff\xfe", "not valid JSON"),
        (b"[]", "must be a JSON object"),
        (b'{"run": "r"}', "unsupported schema version"),
        (b'{"v": 2, "run": "r"}', "unsupported schema version"),
    ],
)
def test_every_decoder_rejects_malformed_envelopes(payload, fragment):
    for decode in (decode_start, decode_stop, decode_metrics, decode_status):
        with pytest.raises(SchemaError, match=fragment):
            decode(payload)


@pytest.mark.parametrize("fields", [{}, {"run": ""}, {"run": 5}])
def test_commands_need_a_non_empty_run_id(fields):
    for decode in (decode_start, decode_stop):
        with pytest.raises(SchemaError, match="'run' must be a non-empty string"):
            decode(raw(**fields))


def test_start_params_must_be_an_object():
    with pytest.raises(SchemaError, match="'params' must be an object"):
        decode_start(raw(run="r1", params=[1, 2]))


@pytest.mark.parametrize(
    "fields, fragment",
    [
        (dict(run="r", state="paused"), "unknown state"),
        (dict(run="r"), "'state' must be a non-empty string"),
        (dict(run="r", state="failed", exit_code=True), "'exit_code' must be an integer"),
        (dict(run="r", state="failed", exit_code="1"), "'exit_code' must be an integer"),
        (dict(run="r", state="failed", error=5), "'error' must be a string"),
    ],
)
def test_status_validation(fields, fragment):
    with pytest.raises(SchemaError, match=fragment):
        decode_status(raw(**fields))


@pytest.mark.parametrize(
    "points, fragment",
    [
        ("nope", "'points' must be a list"),
        ([1], r"points\[0\] must be an object"),
        ([{"values": {"a": 1}}], r"points\[0\].step must be an integer"),
        ([{"step": True, "values": {"a": 1}}], r"points\[0\].step must be an integer"),
        ([{"step": 1.5, "values": {"a": 1}}], r"points\[0\].step must be an integer"),
        ([{"step": 1, "values": {}}], r"points\[0\].values must be a non-empty object"),
        ([{"step": 1, "values": {"a": "x"}}], r"values.a must be a number"),
        ([{"step": 1, "values": {"a": True}}], r"values.a must be a number"),
    ],
)
def test_metric_validation(points, fragment):
    with pytest.raises(SchemaError, match=fragment):
        decode_metrics(raw(run="r1", points=points))


def test_an_empty_batch_is_valid():
    assert decode_metrics(raw(run="r1", points=[])).points == []


# ----------------------------------------------------------------------- topics


def test_topic_helpers():
    assert start_topic("tuning", "p") == "tuning/p/command/start"
    assert stop_topic("tuning", "p") == "tuning/p/command/stop"
    assert status_topic("tuning", "p", "r1") == "tuning/p/r1/status"
    assert metrics_topic("tuning", "p", "r1") == "tuning/p/r1/metrics"


def test_patterns_match_the_topics_of_every_run_in_the_project_only():
    assert topic_matches(status_pattern("tuning", "p"), status_topic("tuning", "p", "r1"))
    assert topic_matches(metrics_pattern("tuning", "p"), metrics_topic("tuning", "p", "r9"))
    assert not topic_matches(status_pattern("tuning", "p"), status_topic("tuning", "other", "r1"))
    assert not topic_matches(status_pattern("tuning", "p"), metrics_topic("tuning", "p", "r1"))
    # command topics are not run topics
    assert not topic_matches(status_pattern("tuning", "p"), start_topic("tuning", "p"))


def test_run_from_topic_works_for_any_root_depth():
    assert run_from_topic("tuning/p/r1/status") == "r1"
    assert run_from_topic("a/b/c/p/r2/metrics") == "r2"
    with pytest.raises(SchemaError):
        run_from_topic("metrics")