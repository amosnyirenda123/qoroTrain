"""The JSON contract between Tracker, Runner and Tuner, and the topics it travels on.
 
Who publishes what:
 
* Tuner   -> ``<root>/<project>/command/start``   (:class:`StartCommand`)
* Tuner   -> ``<root>/<project>/command/stop``    (:class:`StopCommand`)
* Runner  -> ``<root>/<project>/<run>/status``    (:class:`Status`)
* Tracker -> ``<root>/<project>/<run>/metrics``   (:class:`MetricBatch`)
 
Every payload carries ``"v"``, the schema version. Decoding is strict and raises
:class:`SchemaError` for anything malformed, so handlers can reject bad input in one place.
"""


import json
import time
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1

STATES = ("started", "finished", "failed", "stopped")
TERMINAL_STATES = ("finished", "failed", "stopped")


class SchemaError(ValueError):
    """A payload does not match the message schema"""



# Topics

def start_topic(root: str, project: str) -> str:
    return f"{root}/{project}/command/start"
 
 
def stop_topic(root: str, project: str) -> str:
    return f"{root}/{project}/command/stop"

def status_topic(root: str, project: str, run: str) -> str:
    return f"{root}/{project}/{run}/status"
 
def metrics_topic(root: str, project: str, run: str) -> str:
    return f"{root}/{project}/{run}/metrics"


def status_pattern(root: str, project: str) -> str:
    """Filter matching the status topic of every run in a project."""
    return f"{root}/{project}/+/status"


def metrics_pattern(root: str, project: str) -> str:
    """Filter matching the metrics topic of every run in a project."""
    return f"{root}/{project}/+/metrics"
 
 
def run_from_topic(topic: str) -> str:
    """The run id of a ``.../<run>/status`` or ``.../<run>/metrics`` topic."""
    parts = topic.split("/")
    if len(parts) < 2:
        raise SchemaError(f"not a run topic: {topic!r}")
    return parts[-2]



@dataclass(frozen=True)
class StartCommand:
    run: str
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class StopCommand:
    run: str



@dataclass(frozen=True)
class Status:
    run: str
    state: str
    exit_code: int | None = None
    error: str | None = None
    ts: float = field(default_factory=time.time)
 
    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class MetricPoint:
    step: int
    values: dict[str, float]
    t: float = field(default_factory=time.time)


@dataclass(frozen=True)
class MetricBatch:
    run: str
    points: list[MetricPoint]


RUN_MESSAGE = StartCommand | StopCommand | Status | MetricBatch

def encode(message: RUN_MESSAGE)-> bytes:
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



def _load(payload: bytes) -> dict:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SchemaError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("payload must be a JSON object")
    if data.get("v") != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema version {data.get('v')!r} (expected {SCHEMA_VERSION})")
    return data


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"'{key}' must be a non-empty string")
    return value

def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{what} must be a number, got {value!r}")
    return value


def decode_start(payload: bytes) -> StartCommand:
    data = _load(payload)
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise SchemaError("'params' must be an object")
    return StartCommand(run=_text(data, "run"), params=params)


def decode_stop(payload: bytes) -> StopCommand:
    return StopCommand(run=_text(_load(payload), "run"))


def decode_status(payload: bytes) -> Status:
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








