import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from helpers import FakeConnector, wait_for
from qorotrain.bridges.bridge import Bridge
from qorotrain.config import parse_experiment
from qorotrain.config.environment import ENV_USERNAME
from qorotrain.messages.message import Message
from qorotrain.config.contracts.schema import (
    StartCommand,
    StopCommand,
    decode_status,
    encode,
    start_topic,
    status_pattern,
    stop_topic,
)
from qorotrain.routes.route import Route
from qorotrain.runners.runner import Runner

ROOT, PROJECT = "tuning", "demo"

SCRIPT_OK = "pass"
SCRIPT_FAIL = "import sys; print('boom: bad thing', file=sys.stderr); sys.exit(3)"
SCRIPT_SLOW = "import time; time.sleep(60)"
SCRIPT_STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
SCRIPT_SIGKILLED = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
SCRIPT_ENV = """
import json, os, sys
keys = ["QOROTRAIN_RUN_ID", "QOROTRAIN_PROJECT", "QOROTRAIN_BROKER",
        "QOROTRAIN_TOPIC_ROOT", "QOROTRAIN_USERNAME", "QOROTRAIN_PASSWORD"]
json.dump({k: os.environ.get(k) for k in keys}, open(sys.argv[1] + ".env", "w"))
"""
SCRIPT_TREE = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1] + ".child", "w").write(str(child.pid))
time.sleep(60)
"""


class ScriptRunner(Runner):
    """A Runner that runs a Python snippet instead of papermill (the snippet gets argv[1] = params file)."""

    def __init__(self, script, tmp_path, **kwargs):
        kwargs.setdefault("grace_seconds", 1.0)
        super().__init__(
            source=tmp_path / "unused.ipynb",
            project=PROJECT,
            output_dir=tmp_path / "runs",
            topic_root=ROOT,
            **kwargs,
        )
        self.script = script
        self.runs = tmp_path / "runs"

    def _command(self, command, params_file, output_notebook):
        return [sys.executable, "-c", self.script, str(params_file)]


class SlowStatusConnector(FakeConnector):
    """publish() returns slowly after the status was already delivered, like a slow network write."""

    def publish(self, msg):
        super().publish(msg)
        if msg.topic.endswith("/status"):
            time.sleep(0.3)


class Harness:
    def __init__(self, runner, extra=(), connector=None):
        self.runner = runner
        # loopback: the broker hands our own statuses back to us
        self.fake = connector or FakeConnector(loopback=True)
        self.bridge = Bridge([self.fake], components=[runner, *extra], db_path=":memory:").start()

    def start(self, run, params=None):
        self.fake.deliver(start_topic(ROOT, PROJECT), encode(StartCommand(run, params or {})))

    def stop(self, run):
        self.fake.deliver(stop_topic(ROOT, PROJECT), encode(StopCommand(run)))

    def statuses(self, run):
        decoded = [decode_status(m.payload) for m in self.fake.published if m.topic.endswith("/status")]
        return [s for s in decoded if s.run == run]

    def states(self, run):
        return [s.state for s in self.statuses(run)]

    def last(self, run):
        return self.statuses(run)[-1]

    def wait_for_states(self, run, expected, timeout=10):
        assert wait_for(lambda: self.states(run) == expected, timeout=timeout), self.states(run)


@pytest.fixture
def harness(tmp_path):
    created = []

    def make(script, extra=(), connector=None, **kwargs):
        h = Harness(ScriptRunner(script, tmp_path, **kwargs), extra, connector)
        created.append(h)
        return h

    yield make
    for h in created:
        h.bridge.close()


def alive(pid: int) -> bool:
    """True if the process exists and is not a zombie (zombies are dead but not yet reaped)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


# ------------------------------------------------------------------ happy path


def test_successful_run_reports_started_then_finished(harness):
    h = harness(SCRIPT_OK)
    h.start("r1", {"lr": 0.5})
    h.wait_for_states("r1", ["started", "finished"])
    assert h.last("r1").exit_code == 0
    assert h.runner.active_run is None


def test_params_file_merges_fixed_values_with_the_proposal(harness):
    h = harness(SCRIPT_OK, fixed={"seed": 31, "lr": 0.0})
    h.start("r1", {"lr": 0.5})
    h.wait_for_states("r1", ["started", "finished"])
    assert yaml.safe_load((h.runner.runs / "r1" / "params.yaml").read_text()) == {"seed": 31, "lr": 0.5}


def test_the_notebook_process_gets_its_identity_from_the_environment(harness):
    h = harness(SCRIPT_ENV, broker="broker.lan:1884", username="me", password="s3cret")
    h.start("r9")
    h.wait_for_states("r9", ["started", "finished"])
    seen = json.loads((h.runner.runs / "r9" / "params.yaml.env").read_text())
    assert seen == {
        "QOROTRAIN_RUN_ID": "r9",
        "QOROTRAIN_PROJECT": PROJECT,
        "QOROTRAIN_BROKER": "broker.lan:1884",
        "QOROTRAIN_TOPIC_ROOT": ROOT,
        "QOROTRAIN_USERNAME": "me",
        "QOROTRAIN_PASSWORD": "s3cret",
    }


def test_stale_credentials_in_the_parent_environment_are_not_inherited(harness, monkeypatch):
    monkeypatch.setenv(ENV_USERNAME, "stale")
    h = harness(SCRIPT_ENV)  # configured without credentials
    h.start("r1")
    h.wait_for_states("r1", ["started", "finished"])
    seen = json.loads((h.runner.runs / "r1" / "params.yaml.env").read_text())
    assert seen["QOROTRAIN_USERNAME"] is None


# --------------------------------------------------------------------- failures


def test_failed_run_reports_the_exit_code_and_the_end_of_the_log(harness):
    h = harness(SCRIPT_FAIL)
    h.start("r1")
    h.wait_for_states("r1", ["started", "failed"])
    status = h.last("r1")
    assert status.exit_code == 3
    assert "exit code 3" in status.error and "boom: bad thing" in status.error
    assert len(status.error) < 1100


def test_a_run_killed_by_a_signal_says_so(harness):
    h = harness(SCRIPT_SIGKILLED)
    h.start("r1")
    h.wait_for_states("r1", ["started", "failed"])
    assert "terminated by signal 9" in h.last("r1").error


def test_a_launch_failure_is_reported_instead_of_hanging_the_loop(harness):
    class Broken(ScriptRunner):
        def _command(self, command, params_file, output_notebook):
            return ["/definitely/not/a/program"]

    h = Harness(Broken(SCRIPT_OK, Path(os.environ.get("TMPDIR", "/tmp")) / f"broken-{os.getpid()}"))
    try:
        h.start("r1")
        h.wait_for_states("r1", ["failed"])
        assert "could not launch" in h.last("r1").error
        assert h.runner.active_run is None
    finally:
        h.bridge.close()


def test_malformed_start_commands_are_ignored(harness):
    h = harness(SCRIPT_OK)
    h.fake.deliver(start_topic(ROOT, PROJECT), b"nope")
    h.fake.deliver(start_topic(ROOT, PROJECT), b'{"v": 1}')
    time.sleep(0.3)
    assert not [m for m in h.fake.published if m.topic.endswith("/status")]
    h.start("r1")  # and the runner still works afterwards
    h.wait_for_states("r1", ["started", "finished"])


def test_reusing_a_run_id_fails_instead_of_overwriting(harness):
    h = harness(SCRIPT_OK)
    h.start("r1")
    h.wait_for_states("r1", ["started", "finished"])
    h.start("r1")
    assert wait_for(lambda: h.states("r1")[-1] == "failed")
    assert "could not launch" in h.last("r1").error


# ---------------------------------------------------------------- one at a time


def test_a_second_start_while_busy_fails_cleanly(harness):
    h = harness(SCRIPT_SLOW)
    h.start("r1")
    h.wait_for_states("r1", ["started"])
    h.start("r2")
    h.wait_for_states("r2", ["failed"])
    assert "busy with run r1" in h.last("r2").error
    assert h.runner.active_run == "r1"

    h.stop("r1")
    h.wait_for_states("r1", ["started", "stopped"])


def test_the_slot_is_free_before_the_end_is_announced(harness):
    """The Tuner answers `finished` with the next start immediately; it must never find the runner busy."""

    class Chain:
        def routes(self):
            return [Route(status_pattern(ROOT, PROJECT), self.on_status)]

        def on_status(self, message):
            status = decode_status(message.payload)
            if status.run == "r1" and status.state == "finished":
                h.start("r2")

        def start(self, bridge):
            pass

        def stop(self):
            pass

    # The slow publish keeps the watcher inside publish() long enough for the Tuner's
    # reaction to arrive first, so a wrong order would make r2 fail as "busy".
    h = harness(SCRIPT_OK, extra=[Chain()], connector=SlowStatusConnector(loopback=True))
    h.start("r1")
    h.wait_for_states("r2", ["started", "finished"])
    assert h.states("r1") == ["started", "finished"]


# --------------------------------------------------------------------- stopping


def test_stop_command_ends_the_run_with_status_stopped(harness):
    h = harness(SCRIPT_SLOW)
    h.start("r1")
    h.wait_for_states("r1", ["started"])
    h.stop("r1")
    h.wait_for_states("r1", ["started", "stopped"])
    assert h.last("r1").exit_code == -15  # SIGTERM
    assert h.runner.active_run is None


def test_a_run_that_ignores_sigterm_is_killed_after_the_grace_period(harness):
    h = harness(SCRIPT_STUBBORN, grace_seconds=0.5)
    h.start("r1")
    h.wait_for_states("r1", ["started"])
    time.sleep(0.3)  # let the script install its signal handler
    h.stop("r1")
    h.wait_for_states("r1", ["started", "stopped"], timeout=10)
    assert h.last("r1").exit_code == -9  # SIGKILL


def test_stop_for_another_run_is_ignored(harness):
    h = harness(SCRIPT_SLOW)
    h.start("r1")
    h.wait_for_states("r1", ["started"])
    h.stop("someone-else")
    time.sleep(0.3)
    assert h.runner.active_run == "r1" and h.states("r1") == ["started"]
    h.stop("r1")
    h.wait_for_states("r1", ["started", "stopped"])


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc")
def test_stopping_a_run_also_stops_the_processes_it_spawned(harness):
    h = harness(SCRIPT_TREE)
    h.start("r1")
    child_file = h.runner.runs / "r1" / "params.yaml.child"
    assert wait_for(child_file.exists, timeout=10)
    assert wait_for(lambda: child_file.read_text() != "")
    child = int(child_file.read_text())
    assert alive(child)

    h.stop("r1")
    h.wait_for_states("r1", ["started", "stopped"])
    assert wait_for(lambda: not alive(child), timeout=5)


def test_closing_the_bridge_stops_the_active_run(harness):
    h = harness(SCRIPT_SLOW)
    h.start("r1")
    h.wait_for_states("r1", ["started"])
    proc = h.runner._active.proc
    h.bridge.close()
    assert wait_for(lambda: proc.poll() is not None, timeout=5)


# ---------------------------------------------------------------------- set-up


def test_routes_listen_for_start_and_stop_commands(tmp_path):
    runner = ScriptRunner(SCRIPT_OK, tmp_path)
    assert [r.topic for r in runner.routes()] == ["tuning/demo/command/start", "tuning/demo/command/stop"]


def test_default_command_runs_papermill_with_the_params_file(tmp_path):
    runner = Runner(
        source=tmp_path / "nb.ipynb", project=PROJECT, output_dir=tmp_path / "runs",
        kernel="mykernel", timeout=99, workdir=tmp_path / "work", python="/usr/bin/python3",
    )
    argv = runner._command(StartCommand("r1"), tmp_path / "p.yaml", tmp_path / "out.ipynb")
    assert argv[:3] == ["/usr/bin/python3", "-m", "papermill"]
    assert argv[argv.index("-f") + 1].endswith("p.yaml")
    assert argv[argv.index("-k") + 1] == "mykernel"
    assert argv[argv.index("--execution-timeout") + 1] == "99"
    assert argv[argv.index("--cwd") + 1].endswith("work")
    assert "--no-progress-bar" in argv


def test_from_spec_takes_everything_from_the_experiment_file(tmp_path):
    spec = parse_experiment(
        """
version: 1
application: {name: demo}
bridge:
  topic_root: lab
  connectors: [{host: b.lan, port: 1884, username: u, password: p}]
tuner: {algorithm: random}
runner: {output_dir: ./runs, workdir: ../nbs, kernel: k, timeout: 99}
job:
  source: ../nbs/demo.ipynb
  objective: {metric: reward, direction: maximize}
  hyperparameters: {lr: {type: float, min: 0.001, max: 0.1}}
  fixed: {seed: 5}
"""
    )
    runner = Runner.from_spec(spec, base_dir=tmp_path / "cfg")
    assert runner._project == "demo" and runner._root == "lab"
    assert runner._broker == "b.lan:1884" and (runner._username, runner._password) == ("u", "p")
    assert runner._fixed == {"seed": 5} and runner._kernel == "k" and runner._timeout == 99
    assert runner._output_dir == tmp_path / "cfg" / "runs"
    assert str(runner._source).endswith("nbs/demo.ipynb") and str(runner._workdir).endswith("nbs")
    assert [r.topic for r in runner.routes()][0] == "lab/demo/command/start"