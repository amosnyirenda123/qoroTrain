"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/contracts/schema.py

Description:
    The JSON contract between Tracker, Runner and Tuner, and the topics it travels on.

    Who publishes what:

    * Tuner   -> ``<root>/<project>/command/start``   (:class:`StartCommand`)
    * Tuner   -> ``<root>/<project>/command/stop``    (:class:`StopCommand`)
    * Runner  -> ``<root>/<project>/<run>/status``    (:class:`Status`)
    * Tracker -> ``<root>/<project>/<run>/metrics``   (:class:`MetricBatch`)

    Every payload carries ``"v"``, the schema version. Decoding is strict and raises
    :class:`SchemaError` for anything malformed, so handlers can reject bad input in one place.

    Wire format:
        Compact UTF-8 JSON objects (no whitespace), for example
        ``{"v":1,"run":"a1b2c3-000","state":"finished","ts":1.7e9,"exit_code":0}``.
        Metric values are floats; NaN and infinities are encoded with
        Python's JSON extensions (``NaN``, ``Infinity``) so that divergence
        survives the trip to the Tuner.

    Design:
        The message types are frozen dataclasses with no dependency on the
        broker, so the same contract is used by the notebook process (Tracker),
        the Runner and the Tuner. The topic helper functions are the only place
        topic strings are assembled.

===============================================================================
"""


import json
import time
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1
"""Version stamped into every payload as ``"v"``; decoders reject any other value."""

STATES = ("started", "finished", "failed", "stopped")
"""Every run state the Runner may report."""

TERMINAL_STATES = ("finished", "failed", "stopped")
"""States after which a run produces no further status."""


class SchemaError(ValueError):
    """A payload does not match the message schema"""



# Topics

def start_topic(root: str, project: str) -> str:
    """Topic of start commands for a project: ``<root>/<project>/command/start``."""
    return f"{root}/{project}/command/start"


def stop_topic(root: str, project: str) -> str:
    """Topic of stop commands for a project: ``<root>/<project>/command/stop``."""
    return f"{root}/{project}/command/stop"

def status_topic(root: str, project: str, run: str) -> str:
    """Status topic of one run: ``<root>/<project>/<run>/status``."""
    return f"{root}/{project}/{run}/status"

def metrics_topic(root: str, project: str, run: str) -> str:
    """Metrics topic of one run: ``<root>/<project>/<run>/metrics``."""
    return f"{root}/{project}/{run}/metrics"


def status_pattern(root: str, project: str) -> str:
    """Filter matching the status topic of every run in a project."""
    return f"{root}/{project}/+/status"


def metrics_pattern(root: str, project: str) -> str:
    """Filter matching the metrics topic of every run in a project."""
    return f"{root}/{project}/+/metrics"


def run_from_topic(topic: str) -> str:
    """The run id of a ``.../<run>/status`` or ``.../<run>/metrics`` topic.

    Args:
        topic: A concrete run topic.

    Returns:
        The second-to-last topic level.

    Raises:
        SchemaError: If the topic has fewer than two levels.
    """
    parts = topic.split("/")
    if len(parts) < 2:
        raise SchemaError(f"not a run topic: {topic!r}")
    return parts[-2]



@dataclass(frozen=True)
class StartCommand:
    """Ask the Runner to execute the notebook.

    Attributes:
        run (str): New, unique run identifier (the Runner refuses to reuse
            one).
        params (dict[str, Any]): Tunable parameter values for this run; the
            Runner merges them over ``job.fixed``.
    """

    run: str
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class StopCommand:
    """Ask the Runner to stop a run.

    Attributes:
        run (str): Identifier of the run to stop; ignored unless it is the
            active run.
    """

    run: str



@dataclass(frozen=True)
class Status:
    """Lifecycle event of a run, published by the Runner.

    Attributes:
        run (str): Run identifier.
        state (str): One of :data:`STATES`.
        exit_code (int | None): Process exit code (negative for "killed by
            signal"), on terminal states.
        error (str | None): Failure description with the tail of the
            Papermill log, on ``failed``.
        ts (float): Event time, Unix seconds.
    """

    run: str
    state: str
    exit_code: int | None = None
    error: str | None = None
    ts: float = field(default_factory=time.time)

    @property
    def terminal(self) -> bool:
        """``True`` for ``finished``, ``failed`` and ``stopped``."""
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class MetricPoint:
    """Metric values recorded at one training step.

    Attributes:
        step (int): Step index chosen by the notebook (episode, epoch...).
        values (dict[str, float]): Metric name to value; may contain NaN or
            infinities.
        t (float): Time the point was logged, Unix seconds.
    """

    step: int
    values: dict[str, float]
    t: float = field(default_factory=time.time)


@dataclass(frozen=True)
class MetricBatch:
    """A micro-batch of metric points from one run, published by the Tracker.

    Attributes:
        run (str): Run identifier.
        points (list[MetricPoint]): Points in logging order.
    """

    run: str
    points: list[MetricPoint]


RUN_MESSAGE = StartCommand | StopCommand | Status | MetricBatch
"""Union of every message type that :func:`encode` accepts."""

def encode(message: RUN_MESSAGE)-> bytes:
    """Serialise a message to its versioned JSON payload.

    Optional :class:`Status` fields that are ``None`` are omitted.

    Args:
        message: The message to encode.

    Returns:
        UTF-8 encoded compact JSON, including ``"v"``.

    Raises:
        TypeError: If ``message`` is not one of the contract types.
    """
    match message:
        case StartCommand():
            body = {"run": message.run, "params": message.params}
        case StopCommand():
            body = {"run": message.run}
        case Status():
            body = {"run": message.run, "state": message.state, "ts": message.ts}
            if message.exit_code is not None:
                body["exit_code"] = message.exit_code
            if message.error is not None:
                body["error"] = message.error
        case MetricBatch():
            body = {
                "run": message.run,
                "points": [{"step": p.step, "t": p.t, "values": p.values} for p in message.points]
            }
        case _:
            raise TypeError(f"cannot encode {type(message).__name__}")

    return json.dumps({"v": SCHEMA_VERSION, **body}, separators=(",", ":")).encode("utf-8")



def _load(payload: bytes) -> dict[str, Any]:
    """Parse the JSON envelope and check the schema version.

    Raises:
        SchemaError: If the payload is not a JSON object or ``"v"`` is not
            :data:`SCHEMA_VERSION`.
    """
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SchemaError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("payload must be a JSON object")
    if data.get("v") != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema version {data.get('v')!r} (expected {SCHEMA_VERSION})")
    return data


def _text(data: dict[str, Any], key: str) -> str:
    """Return ``data[key]`` if it is a non-empty string, else raise :class:`SchemaError`."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"'{key}' must be a non-empty string")
    return value

def _number(value: Any, what: str) -> float:
    """Return ``value`` if it is an int or float (not a bool), else raise :class:`SchemaError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{what} must be a number, got {value!r}")
    return value


def decode_start(payload: bytes) -> StartCommand:
    """Decode a start command.

    Args:
        payload: Raw message payload.

    Returns:
        The command; ``params`` defaults to ``{}`` when absent.

    Raises:
        SchemaError: If the envelope is invalid, ``run`` is missing or empty,
            or ``params`` is not an object.
    """
    data = _load(payload)
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise SchemaError("'params' must be an object")
    return StartCommand(run=_text(data, "run"), params=params)


def decode_stop(payload: bytes) -> StopCommand:
    """Decode a stop command.

    Args:
        payload: Raw message payload.

    Returns:
        The command.

    Raises:
        SchemaError: If the envelope is invalid or ``run`` is missing or
            empty.
    """
    return StopCommand(run=_text(_load(payload), "run"))


def decode_status(payload: bytes) -> Status:
    """Decode a run status.

    Args:
        payload: Raw message payload.

    Returns:
        The status; ``ts`` defaults to ``0.0`` when absent.

    Raises:
        SchemaError: If the envelope is invalid, ``run`` or ``state`` is
            missing, ``state`` is not in :data:`STATES`, or ``exit_code``,
            ``error`` or ``ts`` has the wrong type.
    """
    data = _load(payload)
    state = _text(data, "state")
    if state not in STATES:
        raise SchemaError(f"unknown state {state!r} (expected one of {', '.join(STATES)})")
    exit_code = data.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise SchemaError("'exit_code' must be an integer")
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        raise SchemaError("'error' must be a string")
    return Status(
        run=_text(data, "run"),
        state=state,
        exit_code=exit_code,
        error=error,
        ts=float(_number(data.get("ts", 0.0), "'ts'")),
    )


def decode_metrics(payload: bytes) -> MetricBatch:
    """Decode a metric batch.

    Integer metric values are converted to ``float``; metric names to
    ``str``. A point's ``t`` defaults to ``0.0`` when absent.

    Args:
        payload: Raw message payload.

    Returns:
        The batch.

    Raises:
        SchemaError: If the envelope is invalid, ``points`` is not a list, a
            point is not an object, a ``step`` is not an integer, ``values``
            is empty or not an object, or a value or ``t`` is not a number.
    """
    data = _load(payload)
    raw_points = data.get("points")
    if not isinstance(raw_points, list):
        raise SchemaError("'points' must be a list")
    points = []
    for i, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise SchemaError(f"points[{i}] must be an object")
        step = raw.get("step")
        if isinstance(step, bool) or not isinstance(step, int):
            raise SchemaError(f"points[{i}].step must be an integer")
        values = raw.get("values")
        if not isinstance(values, dict) or not values:
            raise SchemaError(f"points[{i}].values must be a non-empty object")
        clean = {str(k): float(_number(v, f"points[{i}].values.{k}")) for k, v in values.items()}
        points.append(MetricPoint(step=step, values=clean, t=float(_number(raw.get("t", 0.0), f"points[{i}].t"))))
    return MetricBatch(run=_text(data, "run"), points=points)
