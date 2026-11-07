# API overview

This page introduces the classes and functions you are most likely to use or
extend. Every public module, class and function is documented in full in the
[API reference](reference/index.md), which is generated from the docstrings.

## Configuration

```python
from qorotrain.config import load_experiment, validate_experiment, ConfigError

spec = load_experiment("experiment.yaml")        # raises ConfigError with every problem
report = validate_experiment(spec, base_dir=".", check_broker=False)
if not report.ok:
    print(report.format())
```

| Name | Role |
|---|---|
| {class}`~qorotrain.config.models.ExperimentSpec` | Validated, typed view of `experiment.yaml` (Pydantic v2). Its sections are `application`, `bridge`, `store`, `tuner`, `runner` and `job`. |
| {func}`~qorotrain.config.loader.load_experiment` / {func}`~qorotrain.config.loader.parse_experiment` | Read a file or a string. They raise {class}`~qorotrain.config.loader.ConfigError` with a list of `path: message` problems. |
| {func}`~qorotrain.config.validate.validate_experiment` | Cross-checks the spec against the notebook and the broker. Returns a {class}`~qorotrain.config.validate.ValidationReport`. |
| {func}`~qorotrain.config.notebook.read_notebook` | Static notebook analysis, including the `parameters` cell. |
| {func}`~qorotrain.config.init.generate_experiment` | Produces a starting YAML. |

## Messaging

```python
from qorotrain.bridges.bridge import Bridge
from qorotrain.connectors.config import MQTTConfig
from qorotrain.messages.message import Message
from qorotrain.routes.route import Route

def show(message: Message) -> None:          # runs on the Bridge's dispatch thread
    print(message.topic, message.payload)

with Bridge.from_config(MQTTConfig(host="localhost"), routes=[Route("demo/#", show)], db_path=":memory:") as bridge:
    bridge.publish(Message("demo/hello", b"hi", qos=1))
```

| Name | Role |
|---|---|
| {class}`~qorotrain.messages.message.Message` | Immutable envelope: `topic`, `payload` (bytes), `headers`, `qos`, `retain`. |
| {class}`~qorotrain.routes.route.Route` | Binds an MQTT topic filter (`+`, `#`) to a handler. |
| {class}`~qorotrain.connectors.connector.Connector` | Abstract broker connection. Implement it to support another broker. |
| {class}`~qorotrain.connectors.connector.MQTTConnector` | Paho-based MQTT v5 implementation. |
| {class}`~qorotrain.bridges.bridge.Bridge` | Hosts {class}`~qorotrain.bridges.component.Component` objects, syncs subscriptions, and dispatches messages on one thread. |
| {class}`~qorotrain.bridges.component.Component` | Protocol: `routes()`, `start(bridge)`, `stop()`. |

### Adding a broker

1. Add a frozen dataclass to {mod}`qorotrain.connectors.config` and include it
   in `BrokerConfig`.
2. Subclass {class}`~qorotrain.connectors.connector.Connector`. `on_message`
   must deliver *every* message to the one callback, and that callback runs on
   your client's thread. Replay subscriptions on reconnect.
3. Add a `case` to {func}`~qorotrain.connectors.factory.create_connector`.
4. To make it configurable from YAML, extend
   {class}`~qorotrain.config.models.ConnectorSpec` (and its `to_config`) and
   describe the new keys in {mod}`qorotrain.config.reference`.

## Wire contract

{mod}`qorotrain.config.contracts.schema` defines the four message types
({class}`~qorotrain.config.contracts.schema.StartCommand`,
{class}`~qorotrain.config.contracts.schema.StopCommand`,
{class}`~qorotrain.config.contracts.schema.Status`,
{class}`~qorotrain.config.contracts.schema.MetricBatch`),
{func}`~qorotrain.config.contracts.schema.encode`, the strict `decode_*`
functions, and the topic helpers. Code outside this module never formats a
topic string itself.

## Execution

| Name | Role |
|---|---|
| {class}`~qorotrain.runners.runner.Runner` | Component that turns start commands into Papermill processes and publishes their status. Build it with {meth}`~qorotrain.runners.runner.Runner.from_spec`. |
| {class}`~qorotrain.trackers.tracker.Tracker` | Notebook-side telemetry: `log(step, **metrics)`, `finish()`, context manager. Configured from `QOROTRAIN_*` variables. |

## Tuning

| Name | Role |
|---|---|
| {class}`~qorotrain.tuners.tuner.Tuner` | Abstract component that owns the loop. Subclasses implement only {meth}`~qorotrain.tuners.tuner.Tuner.propose`. Control it with `begin()`, `wait()` and `abort()`; read `history`, `best` and `stop_reason`. |
| {class}`~qorotrain.tuners.tuner.RandomTuner`, {class}`~qorotrain.tuners.tuner.GridTuner` | Implemented algorithms. |
| {class}`~qorotrain.tuners.tuner.StopTuning` | Raised by `propose` when the algorithm has nothing left to try. |
| {class}`~qorotrain.tuners.space.SearchSpace` | `validate`, `sample`, `axis` and `grid` over `job.hyperparameters`. |
| {func}`~qorotrain.tuners.objective.aggregate` | Turns a metric stream into a score (`last`, `best`, `mean`, `mean_last_N`). NaN or infinity becomes `None`. |
| {class}`~qorotrain.tuners.types.Trial`, {class}`~qorotrain.tuners.types.Proposal` | What an algorithm reads and returns. |
| {class}`~qorotrain.tuners.review.Reviewer`, {class}`~qorotrain.tuners.review.Decision` | Approve, edit or reject each proposal. |
| {func}`~qorotrain.tuners.factory.create_tuner` | Builds the configured tuner from an `ExperimentSpec`. |

## Persistence

{class}`~qorotrain.store.metric_store.MetricStore` writes runs
(`create_run`, `mark_started`, `finish_run`) and metric batches
(`add_metrics`, which ignores duplicates), and reads them back (`run`, `runs`,
`series`, `metric_names`). It is safe to use from several threads.

## Command line

{func}`qorotrain.cli.main.main` accepts an `argv` list and returns the exit
code, which makes the CLI testable in-process:

```python
from qorotrain.cli.main import main
assert main(["run", "-f", "experiment.yaml", "--dry-run"]) == 0
```
