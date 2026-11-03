# Qorotrain

**Declarative, event-driven LLM agents for automated hyperparameter tuning from streamed training telemetry.**

Describe your experiment in a YAML file. Qorotrain runs your notebook, streams its metrics, and tunes its hyperparameters.

```bash
qorotrainctl run -f experiment.yaml
```

> **Status: in development.** This is a final-year project (PFA) at ENSA Tétouan. The interfaces below describe the intended design and may change while the first version is built. Sections marked *planned* are not implemented yet.
>
> **Version 1 scope:** training jobs are Jupyter notebooks, MQTT is the only broker, and the agent tunes parameter values only (it never edits notebook code).

---

## What it does

Training a model usually means running an experiment, reading the logs, changing a few hyperparameters, and running again. Qorotrain automates that loop:

1. You declare the notebook, the objective, and the hyperparameters in `experiment.yaml`.
2. Qorotrain executes the notebook, which streams metrics over MQTT.
3. A collector summarizes the stream as it arrives.
4. When the run finishes, a tuning algorithm (an LLM agent, random search, or Optuna) proposes new parameters.
5. You approve, edit, or reject the proposal.
6. The runner re-executes the notebook with the approved parameters.

The LLM never runs inside the training loop, so it adds no latency to training.

## Why

- **Manual tuning is error-prone** and does not scale as the number of parameters and the cost per run grow.
- **Automated search sees only the final score.** It cannot tell that a run diverged early or plateaued, and it does not explain its choices.
- **Tuning tools are tied to a framework** or require wrapping your training code.
- **Feedback arrives after the run ends**, not as events.

## Features

- **Declarative.** One YAML file describes the whole experiment, and it can be versioned in Git.
- **Framework-agnostic.** The only contract with your notebook is a small message schema. Evaluated on NumPy and PyTorch workloads (*planned*: scikit-learn).
- **Event-driven.** Components communicate through an MQTT broker (Mosquitto) and react to messages instead of polling.
- **Swappable tuning algorithms.** `agent`, `random`, and `optuna` share the same pipeline, so comparisons isolate the algorithm.
- **Streaming summaries.** Metrics are micro-batched and summarized incrementally (trend, plateau, divergence, downsampled curve), so the agent receives a compact report instead of raw logs.
- **Human in the loop.** Every proposal is shown with a diff and a rationale before anything runs.
- **Validated before it runs.** `qorotrainctl validate` checks the YAML, the notebook, and the broker connection.
- **Persistent state.** Metrics, runs, proposals, decisions, and the routing table are stored in SQLite, and every run keeps its executed notebook.

## Architecture

```
Training notebook ──metrics/status──▶ [ Mosquitto ] ◀──▶ Connector ◀──▶ Bridge ◀──▶ Tuner
 (telemetry client)                         ▲                          (routing table)  │
        ▲                                   │                                         ▼
        └───────── launches ────────── Runner ◀──── command/start ◀──── Review ◀── Tuning algorithm
                                                                          ▲       (agent / random / optuna)
                                                                        User
```

| Component | Responsibility |
|---|---|
| **Telemetry client** | Small library used inside the notebook. Publishes metrics, parameters, and status. No ML dependencies. |
| **Connector** | Broker abstraction. `MQTTConnector` (Paho) is implemented; RabbitMQ and Kafka are out of scope for now. |
| **Bridge** | Manages connectors, keeps the routing table in SQLite, and delivers messages to tuners. |
| **Tuner** | Collects and summarizes telemetry, calls its tuning algorithm, validates proposals, and publishes start commands. |
| **Tuning algorithm** | Decides what to try next from the trial history. |
| **Runner** | Receives start commands and executes the notebook with the given parameters (Papermill). |

### Topics

| Topic | Purpose | QoS |
|---|---|---|
| `tuning/<project>/<run>/status` | Run lifecycle events | 1 |
| `tuning/<project>/<run>/metrics` | Streamed metric points | 0/1 |
| `tuning/<project>/<run>/config` | Parameters of the run | 1 |
| `tuning/<project>/<run>/alert` | Derived events (plateau, divergence) | 1 |
| `tuning/<project>/<run>/proposal` | Proposal and rationale | 1 |
| `tuning/<project>/command/start` | Start command for the runner | 1 |

Crashed runs are detected through the MQTT Last Will.

## Quick start (*planned interface*)

### 1. Start the broker

```yaml
# docker-compose.yml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    ports: ["1883:1883"]
    volumes: ["./mosquitto.conf:/mosquitto/config/mosquitto.conf"]
```

```
# mosquitto.conf (local development only)
listener 1883
allow_anonymous true
```

```bash
docker compose up -d
pip install -e .
```

### 2. Prepare the notebook

Tag one cell as `parameters` (Papermill convention) and keep it to plain assignments. Compute derived values and build objects in the next cell, or injected values will not take effect.

```python
# cell tagged "parameters"
learning_rate = 5e-4
gamma = 0.9
epsilon_decay_steps = 150
num_episodes = 300
seed = 31
```

Add the telemetry calls. The runner passes the run ID and broker address through environment variables (`QOROTRAIN_RUN_ID`, `QOROTRAIN_BROKER`), so the tracker needs no arguments.

```python
from qorotrain import Tracker

tracker = Tracker()
tracker.start(params={"learning_rate": learning_rate, "gamma": gamma})

for ep in range(num_episodes):
    ...
    tracker.log(step=ep, reward=total_reward, loss=avg_loss, epsilon=epsilon)

tracker.finish()
```

### 3. Describe the experiment

```yaml
# experiment.yaml
version: 1
application: {name: dqn-demo}
bridge:
  connectors:
    - {type: mqtt, host: localhost}
tuner: {algorithm: random, max_runs: 8}
job:
  source: dqn-cartpole.ipynb
  objective: {metric: reward, aggregate: mean_last_100, direction: maximize}
  hyperparameters:
    learning_rate: {type: float, min: 1e-5, max: 1e-2, log: true}
```

### 4. Validate and run

```bash
qorotrainctl validate -f experiment.yaml     # checks the YAML, the notebook, and the broker
qorotrainctl run -f experiment.yaml --dry-run
qorotrainctl run -f experiment.yaml
```

`validate` parses the notebook and checks that one cell is tagged `parameters`, that every declared hyperparameter is assigned there, and that the cell contains only plain assignments. The current value of each parameter is read from the notebook, so `initial` is optional.

## Configuration reference

Unknown keys are rejected. Values of the form `${NAME}` are read from environment variables, so secrets stay out of the file.

```yaml
version: 1                          # schema version of this file
application:
  name: dqn-cartpole-tuning
  log_level: info                   # info | debug

bridge:
  workers: 4                        # dispatch workers
  topic_root: tuning
  connectors:                       # a pool; more than one is allowed
    - name: local-mqtt
      type: mqtt
      host: localhost
      port: 1883
      username: ${MQTT_USER}        # optional
      password: ${MQTT_PASSWORD}
      default_qos: 1                # client_id is generated if omitted
  routes:                           # optional per-handler overrides
    - {handler: tuner.on_metrics, qos: 0, batch: 200}

store:
  path: ./qorotrain.db

tuner:
  algorithm: agent                  # agent | random | optuna
  max_runs: 8
  stop: {target: 450, patience: 3}  # target reached, or no improvement in 3 runs
  review: {mode: manual}            # manual | auto
  summarizer:
    flush_every: 200
    flush_seconds: 2.0
    ema: {fast: 0.1, slow: 0.01}
    plateau: {window: 50, epsilon: 0.01}
  agent:
    llm: {provider: ollama, model: "qwen2.5:3b", temperature: 0.2}
    use_notebook_context: true
    retrieval: false

runner:
  workdir: ../notebooks
  kernel: python3
  timeout: 3600                     # seconds, applies per notebook cell
  output_dir: ./runs                # executed notebook saved for every run

job:
  name: dqn
  source: ../notebooks/dqn-cartpole.ipynb
  objective:
    metric: reward
    aggregate: mean_last_100        # how streamed values become one score
    direction: maximize
  hyperparameters:                  # what the tuner may change
    learning_rate:       {type: float, min: 1e-5, max: 1e-2, log: true}
    gamma:               {type: float, min: 0.9, max: 0.999}
    epsilon_decay_steps: {type: int,   min: 100, max: 20000, log: true}
    hidden_size:         {type: int,   choices: [32, 64, 128]}
  fixed:                            # passed to every run, never tuned
    num_episodes: 300
    seed: 31
```

## Tuning algorithms

| Algorithm | Uses | Rationale output |
|---|---|---|
| `random` | Search space only | Seed only |
| `optuna` | Objective values (TPE) | None |
| `agent` | Telemetry summaries, notebook code, trial history | Yes, tied to evidence from the run |

The LLM agent is built with LangGraph. It reads the parsed notebook and the run summary, can call tools to query metric detail, and returns a structured proposal. Retrieval over past trials is *planned*.

## Evaluation plan

The same search space, run budget, seeds, and runner are used for every method, with auto-approve enabled.

| Workload | Framework | Objective |
|---|---|---|
| n-armed bandits | NumPy | Mean reward over the run |
| DQN on CartPole | PyTorch | Mean reward over the last 100 episodes |

| Method | Runs to target | Best objective |
|---|---|---|
| Manual tuning | TBD | TBD |
| Random search | TBD | TBD |
| Optuna | TBD | TBD |
| LLM agent | TBD | TBD |

Results will be added once the experiments are complete.

## Project structure (*proposed*)

```
qorotrain/
├── cli/           # qorotrainctl (run, validate)
├── config/        # YAML schema (Pydantic models), loader, notebook validation
├── client/        # telemetry client (Tracker)
├── connectors/    # Connector base class, MQTTConnector
├── bridge/        # Bridge, routing table, subscription registry
├── collector/     # micro-batching, incremental summaries
├── store/         # SQLite schema and queries
├── tuner/         # Tuner, TuningAlgorithm, algorithms (agent, random, optuna)
├── review/        # CLI review step
├── runner/        # notebook execution (Papermill)
├── examples/      # n-armed-bandits, dqn-cartpole
└── docs/          # report, diagrams
```

## Roadmap

- [ ] YAML schema, loader, and `validate` command
- [ ] Telemetry client and Mosquitto setup
- [ ] Connector, bridge, and SQLite routing table
- [ ] Collector with micro-batching and incremental summaries
- [ ] Runner with Papermill parameter injection
- [ ] Tuner with random-search baseline
- [ ] LLM agent algorithm with human review
- [ ] Optuna baseline
- [ ] Experiments on the bandit and DQN notebooks
- [ ] Retrieval over past trials
- [ ] Mid-run alerts and early stopping
- [ ] Python script jobs, and RabbitMQ and Kafka connectors

## Requirements

- Python 3.10+
- Docker (for Mosquitto) or a local Mosquitto installation
- `paho-mqtt` 2.x (callbacks take a `CallbackAPIVersion` argument)
- An LLM backend for the agent: a hosted model or a local model through Ollama

## License

TODO: choose a license (for example MIT) and add a `LICENSE` file.

## Author

**Amos Nyirenda**, École nationale des sciences appliquées de Tétouan (ENSA), Université Abdelmalek Essaâdi.
Supervised by Prof. Anouar Abtoy.

## Acknowledgments

Built on [Eclipse Mosquitto](https://mosquitto.org), [Eclipse Paho](https://eclipse.dev/paho/), [LangGraph](https://langchain-ai.github.io/langgraph/), [Optuna](https://optuna.org), and [Papermill](https://papermill.readthedocs.io).