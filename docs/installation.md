# Installation

## Requirements

| Requirement | Why |
|---|---|
| Python **3.12 or newer** (`requires-python = ">=3.12"`; the repository pins 3.14 in `.python-version`) | The code uses `match` statements and PEP 604 unions throughout. |
| A POSIX operating system (Linux, macOS) | The Runner gives each run its own process group (`start_new_session`, `os.killpg`) so that stopping a run also stops its children. |
| An MQTT broker, for example Eclipse Mosquitto 2 | All components communicate through it. |
| [Papermill](https://papermill.readthedocs.io) and a Jupyter kernel | The Runner executes the notebook with `python -m papermill`. |
| [uv](https://docs.astral.sh/uv/) (recommended) | The project is managed with uv (`uv.lock`, `uv_build` backend). |

```{warning}
`papermill` and `pyyaml` are imported or invoked by the code but are **not
declared** in `pyproject.toml`. PyYAML is usually installed transitively;
Papermill is not. Install Papermill yourself (see below) or `qorotrainctl run`
reports every run as failed with `No module named papermill`.
```

## Install the package

From the repository root:

```bash
uv sync                      # creates .venv with the runtime and dev dependencies
uv pip install papermill ipykernel
```

Or, without uv:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . papermill ipykernel
```

This installs two console scripts:

| Script | Target | Purpose |
|---|---|---|
| `qorotrainctl` | `qorotrain.cli.main:main` | The command line (`init`, `validate`, `run`). |
| `qorotrain` | `qorotrain:main` | Placeholder that prints a greeting. |

## Make the notebook kernel see Qorotrain

The notebook runs in a Jupyter kernel, which may be a different Python
environment. That kernel must be able to `import qorotrain` (for the Tracker)
and everything else the notebook imports. The simplest way is to register the
project environment as a kernel:

```bash
.venv/bin/python -m ipykernel install --user --name qorotrain
```

Then set `runner.kernel: qorotrain` in `experiment.yaml` (the default is
`python3`).

## Start a broker

For local development, anonymous Mosquitto on the standard port is enough:

```yaml
# docker-compose.yml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    ports: ["1883:1883"]
    volumes: ["./mosquitto.conf:/mosquitto/config/mosquitto.conf"]
```

```text
# mosquitto.conf (local development only)
listener 1883
allow_anonymous true
```

```bash
docker compose up -d
```

```{caution}
TLS is not supported yet. Do not expose an anonymous broker to an untrusted
network. If the broker requires credentials, put them in `experiment.yaml` as
`${MQTT_USER}` / `${MQTT_PASSWORD}` and export the variables; they are
substituted at load time and passed to the notebook through
`QOROTRAIN_USERNAME` / `QOROTRAIN_PASSWORD`.
```

## Environment variables

`qorotrainctl` sets these for each notebook process. You only set them yourself
to run a notebook with telemetry outside `qorotrainctl`.

| Variable | Meaning |
|---|---|
| `QOROTRAIN_RUN_ID` | Run identifier, the `<run>` topic segment. |
| `QOROTRAIN_PROJECT` | `application.name`, the `<project>` topic segment. |
| `QOROTRAIN_BROKER` | `host:port` of the first configured connector. |
| `QOROTRAIN_TOPIC_ROOT` | `bridge.topic_root` (default `tuning`). |
| `QOROTRAIN_USERNAME`, `QOROTRAIN_PASSWORD` | Broker credentials. They are removed from the notebook's environment when none are configured. |

Any `${NAME}` in `experiment.yaml` is replaced with the environment variable
`NAME` when the file is loaded. A missing variable is a configuration error.

## Install the documentation toolchain

```bash
uv sync --group docs
uv run sphinx-build -b html docs docs/_build/html
```

See [Development](development.md#building-the-documentation) for details.
