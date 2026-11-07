## `version`

| Key | Type | Default | What it does |
|---|---|---|---|
| `version` | one of `1` | required | Schema version of this file. Must be `1`. It lets future releases change the format and still recognise old files. |

## `application`

| Key | Type | Default | What it does |
|---|---|---|---|
| `application` | section | required | Identity of the experiment. |
| `application.name` | text, not empty | required | Name of the experiment. It becomes the project part of every MQTT topic (`<topic_root>/<name>/...`), so experiments with different names never see each other's messages. It is also stored with every run. Use letters, digits, `-` and `_` only: the schema does not enforce this yet, but other characters can break topic matching. |
| `application.log_level` | one of `info`, `debug` | `info` | How much `qorotrainctl run` prints to stderr. `info` shows progress (run starting, run finished, why the session stopped). `debug` adds the library's internal messages. |

## `bridge`

| Key | Type | Default | What it does |
|---|---|---|---|
| `bridge` | section | section is optional | Messaging setup: which broker to talk to and how topics are named. The whole section is optional and defaults to one MQTT connector on `localhost:1883`. |
| `bridge.topic_root` | text, not empty | `tuning` | First segment of every topic (`tuning/<project>/...`). Change it to keep several people or environments apart on one shared broker, for example `alice/tuning`. May contain `/`, but no `+` or `#`. The notebook receives it through `QOROTRAIN_TOPIC_ROOT`. |
| `bridge.connectors` | list of connections, not empty | one local MQTT connector | Broker connections, at least one. The Bridge connects to every entry and subscribes on each; what it publishes goes through the first one. The notebook's Tracker connects to the first entry only, so the first connector must be reachable from the machine running the notebook. Only MQTT exists today. |
| `bridge.connectors[].name` | text | none | **Label only.** Optional label used in messages, for example `cannot reach mqtt broker 'lab'`. Must be unique. Without it, messages say `connector-0`, `connector-1`... |
| `bridge.connectors[].type` | one of `mqtt` | `mqtt` | Kind of broker. Only `mqtt` is implemented. RabbitMQ and Kafka are out of scope for now. |
| `bridge.connectors[].host` | text | `localhost` | Host name or IP address of the broker. |
| `bridge.connectors[].port` | integer, at least 1, at most 65535 | `1883` | TCP port of the broker. 1883 is the standard unencrypted MQTT port. TLS is not supported yet, so do not expose the broker to an untrusted network. |
| `bridge.connectors[].client_id` | text | empty text | MQTT client ID of the Bridge's connection. Empty means a unique `qorotrain-<random>` ID is generated. Set it only if your broker's access rules require a fixed ID: two clients with the same ID disconnect each other. The notebook's Tracker always generates its own ID. |
| `bridge.connectors[].username` | text | none | Broker username, if the broker requires one. Also handed to the notebook through `QOROTRAIN_USERNAME`. |
| `bridge.connectors[].password` | text | none | Broker password. Write it as `${MQTT_PASSWORD}` so the secret stays out of the file. Also handed to the notebook through `QOROTRAIN_PASSWORD`. |
| `bridge.connectors[].default_qos` | one of `0`, `1`, `2` | `1` | Fallback MQTT quality of service: 0 is at most once, 1 is at least once, 2 is exactly once. Every Qorotrain message sets QoS 1 itself, so changing this has no visible effect today. QoS 1 can deliver a message twice, and Qorotrain tolerates that. |

## `store`

| Key | Type | Default | What it does |
|---|---|---|---|
| `store` | section | section is optional | Where results are kept. |
| `store.path` | text | `./qorotrain.db` | SQLite file that holds every run and its metrics. Relative paths are relative to the folder containing `experiment.yaml`, and missing folders are created. A second file, `bridge.db` (connector and route status), is created next to it. Nothing is deleted automatically: runs from earlier sessions stay in the file. |

## `tuner`

| Key | Type | Default | What it does |
|---|---|---|---|
| `tuner` | section | required | How the session searches for good parameters, and when it stops. |
| `tuner.algorithm` | one of `agent`, `random`, `grid`, `optuna` | required | How the next set of parameters is chosen. `random` samples the search space. `grid` interpolates between each range's min and max. `agent` and `optuna` are accepted by the schema but not implemented yet: `validate` warns, and `run` exits with an error. |
| `tuner.max_runs` | integer, at least 1 | `8` | Maximum number of runs in the session, counting the baseline run. With the default `8` you get the baseline plus 7 proposals. The session can stop earlier (target, patience, failures, or a used-up grid). |
| `tuner.seed` | integer | `0` | Seed of the random generator for `random` and `grid` (the grid uses it to shuffle its visiting order). The same seed and the same search space give the same proposals. Ignored by other algorithms. |
| `tuner.baseline` | true or false | `true` | If true, run 0 executes the notebook with its own current values from the `parameters` cell, before anything is tuned. That gives you a reference score to beat. It counts toward `max_runs`. If false, run 0 is the algorithm's first proposal. |
| `tuner.grid_points` | integer, at least 2 | `3` | `grid` only. Number of values taken from each numeric range, interpolated between `min` and `max` (evenly spaced, or evenly spaced in log space for `log` ranges). Integer ranges are rounded and de-duplicated. Ranges given as `choices` ignore this and contribute all their values. The grid may not exceed 100,000 cells. |
| `tuner.stop` | section | section is optional | Optional early-stop rules, on top of `max_runs`. |
| `tuner.stop.target` | number | none | Stop as soon as the best score reaches this value: at least this high when maximizing, at most this low when minimizing. Use it when you know what is good enough. |
| `tuner.stop.patience` | integer, at least 1 | none | Stop after this many runs in a row without a new best score. |
| `tuner.review` | section | section is optional | Whether a person checks each proposal before it runs. |
| `tuner.review.mode` | one of `manual`, `auto` | `manual` | `auto` approves every proposal. `manual` is meant to show each proposal and let a person approve, edit or reject it, but it is not implemented yet: `run` exits with an error. The default is `manual`, so set `auto` for now. |
| `tuner.summarizer` | section | section is optional | **Not used yet.** Settings of the telemetry summarizer that will feed the LLM agent. Validated, but nothing reads them yet (`random` and `grid` do not need summaries). |
| `tuner.summarizer.flush_every` | integer, at least 1 | `200` | **Not used yet.** Planned: how many metric points to collect before updating the summary. |
| `tuner.summarizer.flush_seconds` | number, greater than 0 | `2.0` | **Not used yet.** Planned: the longest time, in seconds, to wait before updating the summary. |
| `tuner.summarizer.ema` | section | section is optional | **Not used yet.** Planned: two exponential moving averages of a metric, whose difference is the trend. |
| `tuner.summarizer.ema.fast` | number, greater than 0, at most 1 | `0.1` | **Not used yet.** Planned: smoothing factor of the fast average, between 0 and 1. Must be larger than `slow`. |
| `tuner.summarizer.ema.slow` | number, greater than 0, at most 1 | `0.01` | **Not used yet.** Planned: smoothing factor of the slow average, between 0 and 1. |
| `tuner.summarizer.plateau` | section | section is optional | **Not used yet.** Planned: detection of a metric that has stopped improving. |
| `tuner.summarizer.plateau.window` | integer, at least 2 | `50` | **Not used yet.** Planned: how many recent points are examined for a plateau. |
| `tuner.summarizer.plateau.epsilon` | number, greater than 0 | `0.01` | **Not used yet.** Planned: relative improvement below which a metric counts as plateaued. |
| `tuner.agent` | section | required if algorithm is agent | **Not used yet.** Settings of the LLM agent. Required when `algorithm` is `agent`, ignored otherwise. The agent is not implemented yet, so nothing reads these settings. |
| `tuner.agent.llm` | section | required | **Not used yet.** Which language model the agent talks to. |
| `tuner.agent.llm.provider` | text | `ollama` | **Not used yet.** Planned: where the model runs, for example `ollama`. |
| `tuner.agent.llm.model` | text, not empty | required | **Not used yet.** Planned: model name, for example `qwen2.5:3b`. |
| `tuner.agent.llm.temperature` | number, at least 0, at most 2 | `0.2` | **Not used yet.** Planned: sampling temperature, from 0 (most deterministic) to 2. |
| `tuner.agent.use_notebook_context` | true or false | `true` | **Not used yet.** Planned: whether the notebook's code cells are included in the agent's prompt. |
| `tuner.agent.retrieval` | true or false | `false` | **Not used yet.** Planned: whether similar past trials are retrieved and shown to the agent. |

## `runner`

| Key | Type | Default | What it does |
|---|---|---|---|
| `runner` | section | section is optional | How the notebook is executed. |
| `runner.workdir` | text | none | Working directory of the notebook's kernel (Papermill's `--cwd`). Relative paths are relative to `experiment.yaml`. Without it the kernel starts in the directory you ran `qorotrainctl` from. Set it when the notebook opens files by relative path. |
| `runner.kernel` | text | `python3` | Name of the Jupyter kernel that runs the notebook (see `jupyter kernelspec list`). That kernel's environment must be able to import `qorotrain` and everything the notebook uses. |
| `runner.timeout` | integer, at least 1 | `3600` | Seconds a single notebook cell may run (Papermill's `--execution-timeout`). It applies per cell, not per run: a training loop that lives in one cell must finish within this time, or the run fails. |
| `runner.output_dir` | text | `./runs` | Folder for per-run files: `<output_dir>/<run_id>/run.ipynb` (the executed notebook with its outputs), `params.yaml` and `papermill.log`. Relative to `experiment.yaml`. Created automatically, never cleaned automatically. |

## `job`

| Key | Type | Default | What it does |
|---|---|---|---|
| `job` | section | required | What is being tuned: the notebook, what counts as success, and which parameters may change. |
| `job.name` | text | none | **Label only.** Optional label for the job. Nothing reads it yet. |
| `job.source` | text, not empty | required | Path of the notebook (`.ipynb`), relative to the folder containing `experiment.yaml`. It needs one cell tagged `parameters` and kernel metadata (any notebook saved by Jupyter has it). |
| `job.objective` | section | required | What counts as success. |
| `job.objective.metric` | text, not empty | required | Name of the metric the notebook logs with `tracker.log(step, <metric>=value)` and that defines success. `validate` warns when no log call uses this name. The placeholder `TODO` written by `init` is rejected. |
| `job.objective.aggregate` | text | `last` | How a run's stream of values becomes one score. `last` is the final value. `best` is the best value seen (highest or lowest depending on `direction`, ignoring NaN). `mean` is the average of all values. `mean_last_N` is the average of the last N values, for example `mean_last_100` (all values if there are fewer than N). A NaN or infinite result means the run diverged and counts as failed. |
| `job.objective.direction` | one of `maximize`, `minimize` | required | `maximize` if a higher score is better, `minimize` if a lower one is. It decides which run is best and how `target` and `patience` are read. |
| `job.hyperparameters` | mapping of name to range, not empty | required | The parameters the tuner may change: a mapping from a variable name to its search range. Every name must be a variable assigned in the notebook's `parameters` cell. At least one is required. |
| `job.hyperparameters.<name>` | section | required | The search range of one parameter, named after the notebook variable. |
| `job.hyperparameters.<name>.type` | one of `float`, `int` | required | `float` or `int`. With `int`, proposals are whole numbers and the bounds must be whole numbers too. |
| `job.hyperparameters.<name>.min` | number | none | Lower bound of the range, inclusive. Use together with `max`; `min` must be smaller than `max`. Not allowed together with `choices`. |
| `job.hyperparameters.<name>.max` | number | none | Upper bound of the range, inclusive. Use together with `min`. Not allowed together with `choices`. |
| `job.hyperparameters.<name>.choices` | list of numbers, at least 2 items | none | An explicit list of allowed values (at least two), for example `[32, 64, 128]`, instead of a range. Not allowed together with `min`, `max` or `log`. |
| `job.hyperparameters.<name>.log` | true or false | `false` | Treat the range on a logarithmic scale, for sampling and for grid interpolation. Use it for values that span orders of magnitude, such as a learning rate. Needs `min` greater than 0. |
| `job.fixed` | mapping | `{}` | Parameters passed to every run but never tuned, such as a seed or the number of episodes. Every name must be a variable assigned in the `parameters` cell, and must not also appear under `hyperparameters`. The value here replaces the notebook's own value in every run. |
