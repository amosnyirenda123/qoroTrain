"""`qorotrainctl run` end to end: real Mosquitto, real Papermill and kernels, the real CLI.

Skipped unless a broker is on localhost:1883 and papermill plus a ``python3`` kernel are installed.
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import nbformat
import pytest

from helpers import wait_for
from nbhelpers import write_notebook
from qorotrain.cli.main import main
from qorotrain.store import MetricStore

pytestmark = pytest.mark.broker

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "bandits.ipynb"


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


@pytest.fixture(autouse=True)
def kernel_can_import_qorotrain(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", f"{ROOT / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")


def experiment(tmp_path, *, tuner, fixed="{n_plays: 100, n_arms: 10, n_repeats: 3, seed: 0}", notebook=None):
    """Write experiment.yaml (unique topics, everything under tmp_path) next to a notebook."""
    notebook = notebook or tmp_path / "bandits.ipynb"
    if not notebook.exists():
        notebook.write_bytes(EXAMPLE.read_bytes())
    path = tmp_path / "experiment.yaml"
    path.write_text(
        f"""
version: 1
application: {{name: e2e}}
bridge: {{topic_root: "qorotrain-test/{uuid.uuid4().hex}"}}
tuner: {tuner}
runner: {{output_dir: ./runs, timeout: 120}}
store: {{path: ./qorotrain.db}}
job:
  source: {notebook.name}
  objective: {{metric: avg_reward, aggregate: last, direction: maximize}}
  hyperparameters:
    eps: {{type: float, min: 0.01, max: 1.0, log: true}}
  fixed: {fixed}
"""
    )
    return path


def runs(tmp_path):
    with MetricStore(str(tmp_path / "qorotrain.db")) as store:
        return store.runs("e2e")


def kernel_count() -> int:
    out = subprocess.run(["pgrep", "-fc", "[i]pykernel_launch"], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


# ------------------------------------------------------------------------ tests


def test_random_search_runs_the_baseline_and_proposals_and_reports_the_best(tmp_path, capsys):
    path = experiment(tmp_path, tuner="{algorithm: random, max_runs: 3, seed: 1, review: {mode: auto}}")
    assert main(["run", "-f", str(path)]) == 0

    recorded = runs(tmp_path)
    assert [r["status"] for r in recorded] == ["finished"] * 3
    assert recorded[0]["params"] == {"eps": 0.2}  # run 0 is the notebook's own value
    assert all(r["objective"] is not None and 0.01 <= r["params"]["eps"] <= 1.0 for r in recorded)
    assert all((tmp_path / "runs" / r["run_id"] / "run.ipynb").exists() for r in recorded)

    out = capsys.readouterr().out
    assert "stopped : reached max_runs (3)" in out and "best    :" in out
    best = max(recorded, key=lambda r: r["objective"])
    assert best["run_id"] in out.split("best    :")[1].splitlines()[0]


def test_each_run_received_the_parameters_the_tuner_chose(tmp_path):
    """The notebook prints the eps it was given; it must match what the store says was proposed."""
    path = experiment(tmp_path, tuner="{algorithm: random, max_runs: 3, seed: 2, review: {mode: auto}}")
    assert main(["run", "-f", str(path)]) == 0
    for run in runs(tmp_path):
        executed = nbformat.read(tmp_path / "runs" / run["run_id"] / "run.ipynb", as_version=4)
        printed = "".join(o.get("text", "") for o in executed.cells[-1].outputs)
        assert f"eps={run['params']['eps']}:" in printed


def test_grid_search_visits_the_interpolated_values_and_then_stops(tmp_path, capsys):
    path = experiment(tmp_path, tuner="{algorithm: grid, grid_points: 3, baseline: false, max_runs: 10, review: {mode: auto}}")
    assert main(["run", "-f", str(path)]) == 0
    assert sorted(r["params"]["eps"] for r in runs(tmp_path)) == [0.01, 0.1, 1.0]  # log-spaced between min and max
    assert "grid exhausted (3 configurations)" in capsys.readouterr().out


def test_a_notebook_that_always_fails_ends_the_session_with_the_error_recorded(tmp_path, capsys):
    broken = write_notebook(
        tmp_path / "broken.ipynb",
        [("eps = 0.2\nn_plays = 1", ["parameters"]), ("raise RuntimeError('bug in the notebook')", [])],
    )
    path = experiment(tmp_path, tuner="{algorithm: random, max_runs: 10, review: {mode: auto}}", fixed="{n_plays: 1}", notebook=broken)
    assert main(["run", "-f", str(path)]) == 1  # nothing succeeded

    recorded = runs(tmp_path)
    assert [r["status"] for r in recorded] == ["failed"] * 3
    assert all("bug in the notebook" in r["error"] for r in recorded)
    out = capsys.readouterr().out
    assert "3 consecutive failed runs" in out and "best    : none" in out


def test_ctrl_c_stops_the_session_and_the_active_run(tmp_path):
    slow = {"n_plays": 20000, "n_repeats": 200}  # one run takes minutes
    path = experiment(
        tmp_path, tuner="{algorithm: random, max_runs: 5, review: {mode: auto}}",
        fixed="{n_plays: %d, n_arms: 10, n_repeats: %d, seed: 0}" % (slow["n_plays"], slow["n_repeats"]),
    )
    baseline = kernel_count()
    proc = subprocess.Popen(
        [sys.executable, "-m", "qorotrain", "run", "-f", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy(),
    )
    lines = []

    def read_stderr():
        for line in proc.stderr:
            lines.append(line)

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    try:
        assert wait_for(lambda: any("starting" in l for l in lines), timeout=30), lines
        assert wait_for(lambda: kernel_count() > baseline, timeout=30), "the notebook never started"

        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=40) == 130
        out = proc.stdout.read()
        assert "stopped : interrupted" in out
        assert wait_for(lambda: kernel_count() <= baseline, timeout=15), "a kernel was left running"
    finally:
        if proc.poll() is None:
            proc.kill()