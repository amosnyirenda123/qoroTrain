# Qorotrain documentation

**Qorotrain** tunes the hyperparameters of a Jupyter training notebook from the
telemetry the notebook streams while it trains. You describe the experiment in
one `experiment.yaml`. `qorotrainctl run` then executes the notebook, collects
its metrics over MQTT, scores each run, and launches the next configuration
chosen by a tuning algorithm, until a stop rule is met.

```bash
qorotrainctl init my-notebook.ipynb      # draft experiment.yaml from the notebook
qorotrainctl validate -f experiment.yaml # check YAML, notebook and broker
qorotrainctl run -f experiment.yaml      # tune
```

```{note}
Qorotrain is a final-year project in active development (version 0.1.0).
Working today: the YAML schema, loader and validation, `init`, the Tracker, the
MQTT connector, the Bridge, the Runner, the metric store, and the `random` and
`grid` algorithms with automatic approval. The LLM agent, Optuna, manual review
and the telemetry summarizer are accepted by the schema but **not implemented**.
See [Known gaps](development.md#known-gaps) for details.
```

## Where to start

| If you want to... | Read |
|---|---|
| Install Qorotrain and a broker | [Installation](installation.md) |
| Tune your first notebook | [Usage](usage.md) |
| Look up an `experiment.yaml` key | [Configuration reference](configuration.md) |
| Understand how the pieces fit together | [Architecture](architecture.md) |
| Find which module does what | [Modules](modules.md) |
| Use or extend the Python API | [API overview](api.md) and the [API reference](reference/index.md) |
| Contribute | [Development guide](development.md) |

## Glossary

Experiment
: One `experiment.yaml`: a notebook, an objective, a search space and settings.
  Its `application.name` is the *project* segment of every topic.

Session
: One execution of `qorotrainctl run`. It produces a sequence of runs whose ids
  share a random 6-character prefix, for example `a1b2c3-000`, `a1b2c3-001`.

Run
: One execution of the notebook with one set of parameter values.

Baseline
: Run 0 when `tuner.baseline` is true. It uses the values already written in
  the notebook's `parameters` cell.

Trial
: A finished run as the tuning algorithm sees it: parameters, objective,
  status and a small summary.

Objective
: The single number that scores a run. It is computed from the stream of one
  logged metric by an aggregation rule (`last`, `best`, `mean`, `mean_last_N`).

Proposal
: The next parameter values an algorithm wants to try, with a rationale.

Component
: Anything hosted by the Bridge. Today that is the Tuner and the Runner.

```{toctree}
:maxdepth: 2
:caption: User guide
:hidden:

installation
usage
configuration
```

```{toctree}
:maxdepth: 2
:caption: Design
:hidden:

architecture
modules
```

```{toctree}
:maxdepth: 2
:caption: API
:hidden:

api
reference/index
```

```{toctree}
:maxdepth: 2
:caption: Project
:hidden:

development
```
