# Usage

## 1. Prepare the notebook

Qorotrain changes parameter values through Papermill. Papermill looks for the
cell **tagged `parameters`** and inserts a new cell after it with the new
values. For that to work, the tagged cell must contain **only plain literal
assignments**. Anything computed from them belongs in the next cell, or it is
computed from the old values.

```python
# cell tagged "parameters" (JupyterLab: right sidebar > Common Tools > Cell Tags)
learning_rate = 5e-4
gamma = 0.9
hidden_size = 64
num_episodes = 300
seed = 31
```

Send metrics with the Tracker. It needs no arguments under `qorotrainctl`,
and it does nothing when the notebook runs on its own:

```python
from qorotrain import Tracker

with Tracker() as tracker:          # finish() runs even if training raises
    for episode in range(num_episodes):
        ...
        tracker.log(episode, reward=total_reward, loss=avg_loss)
```

- `step` (the first argument) must be convertible to `int`; every other keyword
  is a metric and must be convertible to `float`.
- NaN and infinite values are sent as they are. A run whose objective is NaN or
  infinite counts as **diverged** and is recorded as failed.
- Points are sent in batches (every 50 points or 1 s by default), so `log` is
  cheap. Telemetry errors become one-time `RuntimeWarning`s and never interrupt
  training.

## 2. Draft `experiment.yaml`

```bash
qorotrainctl init dqn.ipynb            # writes ./experiment.yaml
qorotrainctl init dqn.ipynb -o exp/dqn.yaml --force
```

`init` reads the `parameters` cell without running it and classifies each
value:

- **Fixed:** non-numbers, booleans, and names containing tokens such as `seed`,
  `episodes`, `epochs`, `path`, `device`, `arms`, `classes`.
- **Integers ≥ 2:** tuned over `[value // 2, value * 2]`.
- **Floats in [0.8, 1):** look like discount factors, tuned over
  `[min(value, 0.9), 0.999]`.
- **Other positive floats:** tuned over one decade either side (capped at 1 below
  1), on a log scale when the value is below 0.1 or at least 1.

Every guessed line carries a `# review` comment. The objective metric is
written as `TODO`, and `validate` refuses to proceed until you replace it with
a metric your notebook logs.

## 3. Edit and validate

A complete example lives in `examples/`: `bandits.ipynb`, an epsilon-greedy
n-armed bandit instrumented with the Tracker, and this `bandits.yaml`:

```yaml
version: 1
application: {name: bandits}

tuner:
  algorithm: random
  max_runs: 6           # counts the baseline run (the notebook's own eps = 0.2)
  seed: 0
  review: {mode: auto}

runner: {output_dir: ./runs, timeout: 600}
store: {path: ./qorotrain.db}

job:
  source: bandits.ipynb
  objective: {metric: avg_reward, aggregate: last, direction: maximize}
  hyperparameters:
    eps: {type: float, min: 0.01, max: 1.0, log: true}
  fixed: {n_plays: 1000, n_arms: 10, n_repeats: 20, seed: 0}
```

```bash
qorotrainctl validate -f experiment.yaml
qorotrainctl validate -f experiment.yaml --skip-broker   # offline
```

Validation prints `error:` and `warning:` lines.

- **Errors block a run.** Examples: the notebook is missing, has no kernel
  metadata naming its language (Papermill needs it), or has no
  `parameters` cell; that cell contains statements that are not literal
  assignments; a declared parameter is not assigned there; the metric is still
  `TODO`; the broker is unreachable.
- **Warnings do not block.** Examples: the notebook's current value is outside
  the declared range; a cell variable is neither tuned nor fixed; the notebook
  never imports `qorotrain`; no `.log(...)` call uses the objective metric; the
  algorithm is not implemented yet.

Every key is described in the [configuration reference](configuration.md).
Two settings matter for a first run:

- `tuner.review.mode` defaults to `manual`, which is not implemented yet. Set
  `review: {mode: auto}`.
- `tuner.algorithm` must be `random` or `grid`.

## 4. Run

```bash
qorotrainctl run -f experiment.yaml --dry-run   # validate and print the plan only
qorotrainctl run -f experiment.yaml
```

For the bandits example, the plan is:

```text
experiment : bandits
algorithm  : random (seed 0), up to 6 runs, review auto
objective  : maximize avg_reward (last)
baseline   : eps=0.2
tuning     : eps: float [0.01, 1] log
```

During the session, progress lines (`run <id>: starting`, `run <id> finished:
objective=...`) go to stderr. At the end, a table of runs, the best run and the
stop reason go to stdout:

```text
run         status       objective  params
<session>-000 finished   <score>    eps=0.2
...
best    : <run> (maximized) objective <score>  eps=<value>
stopped : reached max_runs (6)
```

The session stops at the first of these:

1. Three consecutive failed runs.
2. `tuner.stop.target` reached.
3. `tuner.max_runs` runs completed (the baseline counts).
4. `tuner.stop.patience` runs without a new best.
5. The grid is exhausted (`grid` only).
6. The Runner does not answer a start command, even after one re-send 30 s
   later.
7. Ctrl-C. The active run is stopped and the summary is still printed.

| Exit code | Meaning |
|---|---|
| 0 | At least one run produced an objective (or dry run). |
| 1 | Configuration or validation errors, broker unreachable, or no run produced an objective. |
| 2 | Usage error, existing output file for `init`, or an algorithm or review mode that is not available. |
| 130 | Interrupted with Ctrl-C. |

## 5. Inspect the results

| Where | What |
|---|---|
| `store.path` (default `./qorotrain.db`) | Tables `runs` (parameters, status, objective, error, timestamps) and `metrics` (every point). |
| `bridge.db`, next to the store | Connector status and the routing table of the last session. |
| `runner.output_dir/<run>/run.ipynb` | The executed notebook with its outputs. |
| `runner.output_dir/<run>/params.yaml` | The values injected into that run. |
| `runner.output_dir/<run>/papermill.log` | Papermill's output; the tail of it is stored as the run's error on failure. |

```bash
sqlite3 qorotrain.db "SELECT run_id, status, objective, params FROM runs ORDER BY created_at"
```

Nothing is deleted automatically. Runs from earlier sessions stay in the store
and in `output_dir`.

## Programmatic use

The CLI is a thin composition of library objects. This is what `qorotrainctl
run` does, minus validation and printing:

```python
from pathlib import Path

from qorotrain.bridges.bridge import Bridge
from qorotrain.config import load_experiment
from qorotrain.runners.runner import Runner
from qorotrain.store import MetricStore
from qorotrain.tuners.factory import create_tuner, initial_from_notebook

path = Path("experiment.yaml")
spec = load_experiment(path)
base = path.resolve().parent

with MetricStore("qorotrain.db") as store:
    tuner = create_tuner(spec, store, initial=initial_from_notebook(spec, base))
    runner = Runner.from_spec(spec, base)
    bridge = Bridge.from_config(
        [c.to_config() for c in spec.bridge.connectors],
        components=[runner, tuner],
        db_path="bridge.db",
    )
    with bridge:
        tuner.begin()
        tuner.wait()
    print(tuner.stop_reason, tuner.best)
```

### Writing a tuning algorithm

Subclass {class}`~qorotrain.tuners.tuner.Tuner` and implement `propose`. The
base class handles messaging, scoring, stop rules, validation and review.

```python
from qorotrain.tuners import Proposal, StopTuning, Tuner


class HalvingLearningRate(Tuner):
    """Halve the learning rate after every run; stop below the search range.

    Assumes a baseline run (tuner.baseline: true), so history is never empty.
    """

    def propose(self, history):
        lr = history[-1].params["learning_rate"] / 2
        if self._space.params["learning_rate"].accepts(lr):
            raise StopTuning("learning rate left the search range")
        return Proposal({"learning_rate": lr}, "halve the learning rate")
```

A proposal may contain only the parameters that change. The rest is taken
from the previous run. `propose` runs on the Tuner's own thread, so it may be
slow (for example a call to a model).

### Reviewing proposals

Pass an object with a `review(proposal, history) -> Decision` method as
`reviewer=` to `create_tuner`. This is also the only way to use
`tuner.review.mode: manual` today.

```python
from qorotrain.tuners import Decision


class ConsoleReviewer:
    def review(self, proposal, history):
        answer = input(f"run {proposal.params}? [y/N] ")
        return Decision("approve") if answer == "y" else Decision("reject", reason="declined")
```
