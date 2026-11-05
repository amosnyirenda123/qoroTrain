"""End to end: Tuner-side command -> Runner -> Papermill -> kernel -> Tracker -> broker -> collector.

Needs a broker on localhost:1883, ``papermill`` and a ``python3`` Jupyter kernel that can import
qorotrain (the tests put ``src`` on PYTHONPATH). Skipped otherwise.
"""
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from helpers import wait_for
from nbhelpers import write_notebook
from qorotrain.bridges.bridge import Bridge
from qorotrain.connectors.config import MQTTConfig
from qorotrain.messages.message import Message
from qorotrain.config.contracts.schema import (
    StartCommand,
    StopCommand,
    decode_metrics,
    decode_status,
    encode,
    metrics_pattern,
    start_topic,
    status_pattern,
    stop_topic,
)
from qorotrain.routes.route import Route
from qorotrain.runners.runner import Runner

pytestmark = pytest.mark.broker

SRC = Path(__file__).resolve().parents[1] / "src"
PARAMS = "lr = 0.1\nsteps = 5\n"

NB_OK = """
from qorotrain import Tracker
tracker = Tracker()
for i in range(steps):
    tracker.log(i, lr=lr, reward=i * lr)
tracker.finish()
"""
NB_FORGOT_FINISH = """
from qorotrain import Tracker
tracker = Tracker()
for i in range(steps):
    tracker.log(i, lr=lr, reward=i * lr)
"""
NB_CRASH_IN_WITH = """
from qorotrain import Tracker
with Tracker() as tracker:
    tracker.log(0, reward=1.0)
    tracker.log(1, reward=2.0)
    raise RuntimeError("training blew up")
"""
NB_CRASH_NO_FINISH = """
from qorotrain import Tracker
tracker = Tracker()
tracker.log(0, reward=1.0)
tracker.log(1, reward=2.0)
raise RuntimeError("training blew up")
"""
NB_SLOW = """
import time
from qorotrain import Tracker
tracker = Tracker(flush_every=1)
tracker.log(0, reward=1.0)
time.sleep(120)
"""


@pytest.fixture(scope="module", autouse=True)
def require_everything():
    try:
        socket.create_connection(("localhost", 1883), timeout=0.5).close()
    except OSError:
        pytest.skip("No MQTT broker reachable on localhost:1883")
    pytest.importorskip("papermill")
    kernelspec = pytest.importorskip("jupyter_client.kernelspec")
    if "python3" not in kernelspec.KernelSpecManager().find_kernel_specs():
        pytest.skip("no 'python3' Jupyter kernel installed")


class Probe:
    """Stands in for the Tuner's collector: records everything it receives, in arrival order."""

    def __init__(self, root, project):
        self.root, self.project = root, project
        self.statuses, self.points, self.order = [], [], []

    def routes(self):
        return [
            Route(status_pattern(self.root, self.project), self.on_status, qos=1),
            Route(metrics_pattern(self.root, self.project), self.on_metrics, qos=1),
        ]

    def on_status(self, message):
        status = decode_status(message.payload)
        self.statuses.append(status)
        self.order.append(("status", status.run, status.state))

    def on_metrics(self, message):
        batch = decode_metrics(message.payload)
        for point in batch.points:
            self.points.append((batch.run, point.step, point.values))
            self.order.append(("metric", batch.run, point.step))

    def start(self, bridge):
        pass

    def stop(self):
        pass

    def states(self, run):
        return [s.state for s in self.statuses if s.run == run]

    def steps(self, run):
        return [step for r, step, _ in self.points if r == run]


class Stack:
    def __init__(self, bridge, probe, root, project):
        self.bridge, self.probe, self.root, self.project = bridge, probe, root, project

    def start(self, run, params=None):
        topic = start_topic(self.root, self.project)
        self.bridge.publish(Message(topic, encode(StartCommand(run, params or {})), qos=1))

    def stop(self, run):
        self.bridge.publish(Message(stop_topic(self.root, self.project), encode(StopCommand(run)), qos=1))

    def wait_terminal(self, run, timeout=90):
        assert wait_for(
            lambda: any(s.run == run and s.terminal for s in self.probe.statuses), timeout=timeout, interval=0.05
        ), f"run {run} never ended; saw {self.probe.order}"
        return next(s for s in self.probe.statuses if s.run == run and s.terminal)


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    bridges = []

    def make(body):
        root, project = f"qorotrain-test/{uuid.uuid4().hex}", "e2e"
        notebook = write_notebook(tmp_path / "nb.ipynb", [(PARAMS, ["parameters"]), (body, [])])
        runner = Runner(
            source=notebook, project=project, output_dir=tmp_path / "runs", topic_root=root,
            broker="localhost:1883", timeout=120, grace_seconds=3,
        )
        probe = Probe(root, project)
        bridge = Bridge.from_config(MQTTConfig(), components=[runner, probe], db_path=":memory:").start()
        bridges.append(bridge)
        time.sleep(0.4)  # let the subscriptions settle before the first command
        return Stack(bridge, probe, root, project)

    yield make
    for bridge in bridges:
        bridge.close()


def kernel_count() -> int:
    out = subprocess.run(["pgrep", "-fc", "[i]pykernel_launch"], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


# ------------------------------------------------------------------------- tests


def test_full_loop_injects_parameters_streams_metrics_and_reports_the_end(build, tmp_path):
    stack = build(NB_OK)
    stack.start("r1", {"lr": 0.5, "steps": 4})
    end = stack.wait_terminal("r1")

    assert end.state == "finished" and end.exit_code == 0
    assert stack.probe.states("r1") == ["started", "finished"]

    # the proposal reached the notebook: it logged the injected lr, and reward = step * lr
    assert stack.probe.steps("r1") == [0, 1, 2, 3]
    for _, step, values in stack.probe.points:
        assert values == {"lr": 0.5, "reward": step * 0.5}

    assert (tmp_path / "runs" / "r1" / "run.ipynb").exists()  # the executed notebook is kept


def test_every_metric_arrives_before_the_run_is_reported_finished(build):
    stack = build(NB_OK)
    stack.start("r1", {"steps": 30})
    stack.wait_terminal("r1")
    time.sleep(0.5)  # give any straggler the chance to arrive late and expose a reordering

    kinds = [kind for kind, *_ in stack.probe.order]
    last_metric = max(i for i, k in enumerate(kinds) if k == "metric")
    finished = next(i for i, (k, _, state) in enumerate(stack.probe.order) if k == "status" and state == "finished")
    assert stack.probe.steps("r1") == list(range(30))
    assert last_metric < finished


def test_a_notebook_that_forgets_finish_still_delivers_its_last_points(build):
    stack = build(NB_FORGOT_FINISH)
    stack.start("r1", {"steps": 5})
    stack.wait_terminal("r1")
    time.sleep(0.5)
    assert stack.probe.states("r1") == ["started", "finished"]
    assert stack.probe.steps("r1") == [0, 1, 2, 3, 4]


def test_a_crash_inside_a_with_block_delivers_the_points_and_reports_the_error(build):
    stack = build(NB_CRASH_IN_WITH)
    stack.start("r1")
    end = stack.wait_terminal("r1")
    time.sleep(0.5)
    assert end.state == "failed" and "training blew up" in end.error
    assert stack.probe.steps("r1") == [0, 1]


def test_a_crash_without_finish_delivers_the_points_too(build):
    stack = build(NB_CRASH_NO_FINISH)
    stack.start("r1")
    end = stack.wait_terminal("r1")
    time.sleep(0.5)
    assert end.state == "failed" and "training blew up" in end.error
    assert stack.probe.steps("r1") == [0, 1]


def test_stop_ends_a_running_notebook_and_leaves_no_kernel_behind(build):
    baseline = kernel_count()
    stack = build(NB_SLOW)
    stack.start("r1")
    assert wait_for(lambda: stack.probe.steps("r1"), timeout=60), "the notebook never started logging"

    stack.stop("r1")
    end = stack.wait_terminal("r1", timeout=30)
    assert end.state == "stopped"
    assert wait_for(lambda: kernel_count() <= baseline, timeout=10)


def test_two_runs_in_a_row_each_get_their_own_parameters(build):
    stack = build(NB_OK)
    stack.start("r1", {"lr": 0.2, "steps": 2})
    stack.wait_terminal("r1")
    stack.start("r2", {"lr": 0.7, "steps": 2})
    stack.wait_terminal("r2")
    time.sleep(0.3)
    lr = {run: {values["lr"] for r, _, values in stack.probe.points if r == run} for run in ("r1", "r2")}
    assert lr == {"r1": {0.2}, "r2": {0.7}}