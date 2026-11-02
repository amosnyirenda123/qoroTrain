# LoopTune

**Event-driven LLM agents for automated hyperparameter tuning from streamed training telemetry.**

> *LoopTune is a working name; rename it freely.*
>
> **Status: in development.** This is a final-year project (PFA) at ENSA Tétouan. The interfaces below describe the intended design and may change while the first version is built. Sections marked *planned* are not implemented yet.

---

## What it does

Training a model usually means running an experiment, reading the logs, changing a few hyperparameters, and running again. LoopTune automates that loop:

1. Your training script streams metrics over MQTT.
2. A collector summarizes the stream as it arrives.
3. When the run finishes, a tuning algorithm (an LLM agent, random search, or Optuna) proposes new parameters.
4. You approve, edit, or reject the proposal.
5. A runner relaunches the training with the approved parameters.

The LLM never runs inside the training loop, so it adds no latency to training.

## Why

- **Manual tuning is error-prone** and does not scale as the number of parameters and the cost per run grow.
- **Automated search sees only the final score.** It cannot tell that a run diverged early or plateaued, and it does not explain its choices.
- **Tuning tools are tied to a framework** or require wrapping your training code.
- **Feedback arrives after the run ends**, not as events.

## Features

- **Framework-agnostic.** The only contract with your code is a small JSON message schema. Tested on NumPy and PyTorch workloads (*planned*: scikit-learn).
- **Event-driven.** Components communicate through an MQTT broker (Mosquitto) and react to messages instead of polling.
- **Swappable tuning algorithms.** `agent`, `random`, and `optuna` share the same pipeline, so comparisons isolate the algorithm.
- **Streaming summaries.** Metrics are micro-batched and summarized incrementally (trend, plateau, divergence, downsampled curve), so the agent receives a compact report instead of raw logs.
- **Human in the loop.** Every proposal is shown with a diff and a rationale before anything runs.
- **Persistent state.** Metrics, runs, proposals, decisions, and the routing table are stored in SQLite.

## Architecture

```
Training job ──metrics/status──▶ [ Mosquitto ] ◀──▶ Connector ◀──▶ Bridge ◀──▶ Tuner
 (telemetry client)                    ▲                           (routing table)  │
        ▲                              │                                         ▼
        └────────── launches ───── Runner ◀──── command/start ◀──── Review ◀── Tuning algorithm
                                                                      ▲        (agent / random / optuna)
                                                                    User
```

| Component | Responsibility |
|---|---|
| **Telemetry client** | Small library used inside the training code. Publishes metrics, parameters, and status. No ML dependencies. |
| **Connector** | Broker abstraction. `MQTTConnector` (Paho) is implemented; RabbitMQ and Kafka are out of scope for now. |
| **Bridge** | Manages connectors, keeps the routing table in SQLite, and delivers messages to tuners. |
| **Tuner** | Collects and summarizes telemetry, calls its tuning algorithm, validates proposals, and publishes start commands. |
| **Tuning algorithm** | Decides what to try next from the trial history. |
| **Runner** | Receives start commands and launches the notebook or script with the given parameters. |

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

### 2. Instrument your training

```python
from looptune import Tracker

tracker = Tracker(project="dqn-cartpole", broker="localhost:1883")
tracker.start(params={"learning_rate": lr, "gamma": gamma})

for ep in range(num_episodes):
    ...
    tracker.log(step=ep, reward=total_reward, loss=avg_loss, epsilon=epsilon)

tracker.finish(objective=mean_reward_last_100)
```

### 3. Parameterize the notebook

Tag one cell as `parameters` (Papermill convention) and keep it to plain assignments. Compute derived values and build objects in the next cell, or injected values will not take effect.

```python
learning_rate = 5e-4
gamma = 0.9
epsilon_decay_steps = 150
num_episodes = 300
seed = 31
```

### 4. Declare the search space

```yaml
# space.yaml
objective: {metric: mean_reward_last_100, direction: maximize}
params:
  learning_rate:       {type: float, min: 1e-5, max: 1e-2, log: true}
  gamma:               {type: float, min: 0.9, max: 0.999}
  epsilon_decay_steps: {type: int,   min: 100, max: 20000, log: true}
```

Every proposal is validated against this file before it reaches the runner.

### 5. Run the loop

```bash
looptune run dqn-cartpole.ipynb --space space.yaml --algorithm agent --max-runs 8
```

Use `--algorithm random` or `--algorithm optuna` for the baselines, and `--auto-approve` for unattended experiments.

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
looptune/
├── client/        # telemetry client (Tracker)
├── connectors/    # Connector base class, MQTTConnector
├── bridge/        # Bridge, routing table, subscription registry
├── collector/     # micro-batching, incremental summaries
├── store/         # SQLite schema and queries
├── tuner/         # Tuner, TuningAlgorithm, algorithms (agent, random, optuna)
├── review/        # CLI review step
├── runner/        # notebook and script launcher (Papermill)
├── examples/      # n-armed-bandits, dqn-cartpole
└── docs/          # report, diagrams
```

## Roadmap

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
- [ ] RabbitMQ and Kafka connectors

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