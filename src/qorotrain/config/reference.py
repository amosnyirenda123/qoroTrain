"""Reference documentation of ``experiment.yaml``, generated from the schema models.

The schema (``models.py``) is the single source of truth for keys, types, defaults and
constraints. This module adds the one thing the schema cannot know: what each key *means*.
``tests/test_reference.py`` fails when a key exists without a description (or the other way
round), so the documentation cannot silently fall behind the code.

Regenerate the checked-in page with::

    python -m qorotrain.config.reference > docs/experiment-yaml-reference.md
"""
import types
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from .models import ExperimentSpec

ACTIVE, RESERVED, LABEL = "active", "reserved", "label"


@dataclass(frozen=True)
class Entry:
    meaning: str
    status: str = ACTIVE  # active: read by the code. reserved: validated but unused. label: only shown in messages
    required: str | None = None  # overrides the computed "required" / "optional"


DESCRIPTIONS: dict[str, Entry] = {
    # ---------------------------------------------------------------- version
    "version": Entry("Schema version of this file. Must be `1`. It lets future releases change the format and still recognise old files."),
    # ------------------------------------------------------------ application
    "application": Entry("Identity of the experiment."),
    "application.name": Entry(
        "Name of the experiment. It becomes the project part of every MQTT topic (`<topic_root>/<name>/...`), so experiments with different names never see each other's messages. It is also stored with every run. "
        "Use letters, digits, `-` and `_` only: the schema does not enforce this yet, but other characters can break topic matching."
    ),
    "application.log_level": Entry("How much `qorotrainctl run` prints to stderr. `info` shows progress (run starting, run finished, why the session stopped). `debug` adds the library's internal messages."),
    # ----------------------------------------------------------------- bridge
    "bridge": Entry("Messaging setup: which broker to talk to and how topics are named. The whole section is optional and defaults to one MQTT connector on `localhost:1883`."),
    "bridge.topic_root": Entry(
        "First segment of every topic (`tuning/<project>/...`). Change it to keep several people or environments apart on one shared broker, for example `alice/tuning`. May contain `/`, but no `+` or `#`. The notebook receives it through `QOROTRAIN_TOPIC_ROOT`."
    ),
    "bridge.connectors": Entry(
        "Broker connections, at least one. The Bridge connects to every entry and subscribes on each; what it publishes goes through the first one. "
        "The notebook's Tracker connects to the first entry only, so the first connector must be reachable from the machine running the notebook. Only MQTT exists today.",
    ),
    "bridge.connectors[].name": Entry("Optional label used in messages, for example `cannot reach mqtt broker 'lab'`. Must be unique. Without it, messages say `connector-0`, `connector-1`...", LABEL),
    "bridge.connectors[].type": Entry("Kind of broker. Only `mqtt` is implemented. RabbitMQ and Kafka are out of scope for now."),
    "bridge.connectors[].host": Entry("Host name or IP address of the broker."),
    "bridge.connectors[].port": Entry("TCP port of the broker. 1883 is the standard unencrypted MQTT port. TLS is not supported yet, so do not expose the broker to an untrusted network."),
    "bridge.connectors[].client_id": Entry(
        "MQTT client ID of the Bridge's connection. Empty means a unique `qorotrain-<random>` ID is generated. Set it only if your broker's access rules require a fixed ID: two clients with the same ID disconnect each other. "
        "The notebook's Tracker always generates its own ID."
    ),
    "bridge.connectors[].username": Entry("Broker username, if the broker requires one. Also handed to the notebook through `QOROTRAIN_USERNAME`."),
    "bridge.connectors[].password": Entry("Broker password. Write it as `${MQTT_PASSWORD}` so the secret stays out of the file. Also handed to the notebook through `QOROTRAIN_PASSWORD`."),
    "bridge.connectors[].default_qos": Entry(
        "Fallback MQTT quality of service: 0 is at most once, 1 is at least once, 2 is exactly once. Every Qorotrain message sets QoS 1 itself, so changing this has no visible effect today. "
        "QoS 1 can deliver a message twice, and Qorotrain tolerates that."
    ),
    # ------------------------------------------------------------------ store
    "store": Entry("Where results are kept."),
    "store.path": Entry(
        "SQLite file that holds every run and its metrics. Relative paths are relative to the folder containing `experiment.yaml`, and missing folders are created. A second file, `bridge.db` (connector and route status), is created next to it. "
        "Nothing is deleted automatically: runs from earlier sessions stay in the file."
    ),
    # ------------------------------------------------------------------ tuner
    "tuner": Entry("How the session searches for good parameters, and when it stops."),
    "tuner.algorithm": Entry(
        "How the next set of parameters is chosen. `random` samples the search space. `grid` interpolates between each range's min and max. "
        "`agent` and `optuna` are accepted by the schema but not implemented yet: `validate` warns, and `run` exits with an error."
    ),
    "tuner.max_runs": Entry("Maximum number of runs in the session, counting the baseline run. With the default `8` you get the baseline plus 7 proposals. The session can stop earlier (target, patience, failures, or a used-up grid)."),
    "tuner.seed": Entry("Seed of the random generator for `random` and `grid` (the grid uses it to shuffle its visiting order). The same seed and the same search space give the same proposals. Ignored by other algorithms."),
    "tuner.baseline": Entry("If true, run 0 executes the notebook with its own current values from the `parameters` cell, before anything is tuned. That gives you a reference score to beat. It counts toward `max_runs`. If false, run 0 is the algorithm's first proposal."),
    "tuner.grid_points": Entry(
        "`grid` only. Number of values taken from each numeric range, interpolated between `min` and `max` (evenly spaced, or evenly spaced in log space for `log` ranges). Integer ranges are rounded and de-duplicated. "
        "Ranges given as `choices` ignore this and contribute all their values. The grid may not exceed 100,000 cells."
    ),
    "tuner.stop": Entry("Optional early-stop rules, on top of `max_runs`."),
    "tuner.stop.target": Entry("Stop as soon as the best score reaches this value: at least this high when maximizing, at most this low when minimizing. Use it when you know what is good enough."),
    "tuner.stop.patience": Entry("Stop after this many runs in a row without a new best score."),
    "tuner.review": Entry("Whether a person checks each proposal before it runs."),
    "tuner.review.mode": Entry(
        "`auto` approves every proposal. `manual` is meant to show each proposal and let a person approve, edit or reject it, but it is not implemented yet: `run` exits with an error. "
        "The default is `manual`, so set `auto` for now."
    ),
    "tuner.summarizer": Entry("Settings of the telemetry summarizer that will feed the LLM agent. Validated, but nothing reads them yet (`random` and `grid` do not need summaries).", RESERVED),
    "tuner.summarizer.flush_every": Entry("Planned: how many metric points to collect before updating the summary.", RESERVED),
    "tuner.summarizer.flush_seconds": Entry("Planned: the longest time, in seconds, to wait before updating the summary.", RESERVED),
    "tuner.summarizer.ema": Entry("Planned: two exponential moving averages of a metric, whose difference is the trend.", RESERVED),
    "tuner.summarizer.ema.fast": Entry("Planned: smoothing factor of the fast average, between 0 and 1. Must be larger than `slow`.", RESERVED),
    "tuner.summarizer.ema.slow": Entry("Planned: smoothing factor of the slow average, between 0 and 1.", RESERVED),
    "tuner.summarizer.plateau": Entry("Planned: detection of a metric that has stopped improving.", RESERVED),
    "tuner.summarizer.plateau.window": Entry("Planned: how many recent points are examined for a plateau.", RESERVED),
    "tuner.summarizer.plateau.epsilon": Entry("Planned: relative improvement below which a metric counts as plateaued.", RESERVED),
    "tuner.agent": Entry("Settings of the LLM agent. Required when `algorithm` is `agent`, ignored otherwise. The agent is not implemented yet, so nothing reads these settings.", RESERVED, required="required if algorithm is agent"),
    "tuner.agent.llm": Entry("Which language model the agent talks to.", RESERVED),
    "tuner.agent.llm.provider": Entry("Planned: where the model runs, for example `ollama`.", RESERVED),
    "tuner.agent.llm.model": Entry("Planned: model name, for example `qwen2.5:3b`.", RESERVED),
    "tuner.agent.llm.temperature": Entry("Planned: sampling temperature, from 0 (most deterministic) to 2.", RESERVED),
    "tuner.agent.use_notebook_context": Entry("Planned: whether the notebook's code cells are included in the agent's prompt.", RESERVED),
    "tuner.agent.retrieval": Entry("Planned: whether similar past trials are retrieved and shown to the agent.", RESERVED),
    # ----------------------------------------------------------------- runner
    "runner": Entry("How the notebook is executed."),
    "runner.workdir": Entry("Working directory of the notebook's kernel (Papermill's `--cwd`). Relative paths are relative to `experiment.yaml`. Without it the kernel starts in the directory you ran `qorotrainctl` from. Set it when the notebook opens files by relative path."),
    "runner.kernel": Entry("Name of the Jupyter kernel that runs the notebook (see `jupyter kernelspec list`). That kernel's environment must be able to import `qorotrain` and everything the notebook uses."),
    "runner.timeout": Entry("Seconds a single notebook cell may run (Papermill's `--execution-timeout`). It applies per cell, not per run: a training loop that lives in one cell must finish within this time, or the run fails."),
    "runner.output_dir": Entry("Folder for per-run files: `<output_dir>/<run_id>/run.ipynb` (the executed notebook with its outputs), `params.yaml` and `papermill.log`. Relative to `experiment.yaml`. Created automatically, never cleaned automatically."),
    # -------------------------------------------------------------------- job
    "job": Entry("What is being tuned: the notebook, what counts as success, and which parameters may change."),
    "job.name": Entry("Optional label for the job. Nothing reads it yet.", LABEL),
    "job.source": Entry("Path of the notebook (`.ipynb`), relative to the folder containing `experiment.yaml`. It needs one cell tagged `parameters` and kernel metadata (any notebook saved by Jupyter has it)."),
    "job.objective": Entry("What counts as success."),
    "job.objective.metric": Entry("Name of the metric the notebook logs with `tracker.log(step, <metric>=value)` and that defines success. `validate` warns when no log call uses this name. The placeholder `TODO` written by `init` is rejected."),
    "job.objective.aggregate": Entry(
        "How a run's stream of values becomes one score. `last` is the final value. `best` is the best value seen (highest or lowest depending on `direction`, ignoring NaN). `mean` is the average of all values. "
        "`mean_last_N` is the average of the last N values, for example `mean_last_100` (all values if there are fewer than N). A NaN or infinite result means the run diverged and counts as failed."
    ),
    "job.objective.direction": Entry("`maximize` if a higher score is better, `minimize` if a lower one is. It decides which run is best and how `target` and `patience` are read."),
    "job.hyperparameters": Entry("The parameters the tuner may change: a mapping from a variable name to its search range. Every name must be a variable assigned in the notebook's `parameters` cell. At least one is required."),
    "job.hyperparameters.<name>": Entry("The search range of one parameter, named after the notebook variable."),
    "job.hyperparameters.<name>.type": Entry("`float` or `int`. With `int`, proposals are whole numbers and the bounds must be whole numbers too."),
    "job.hyperparameters.<name>.min": Entry("Lower bound of the range, inclusive. Use together with `max`; `min` must be smaller than `max`. Not allowed together with `choices`."),
    "job.hyperparameters.<name>.max": Entry("Upper bound of the range, inclusive. Use together with `min`. Not allowed together with `choices`."),
    "job.hyperparameters.<name>.choices": Entry("An explicit list of allowed values (at least two), for example `[32, 64, 128]`, instead of a range. Not allowed together with `min`, `max` or `log`."),
    "job.hyperparameters.<name>.log": Entry("Treat the range on a logarithmic scale, for sampling and for grid interpolation. Use it for values that span orders of magnitude, such as a learning rate. Needs `min` greater than 0."),
    "job.fixed": Entry(
        "Parameters passed to every run but never tuned, such as a seed or the number of episodes. Every name must be a variable assigned in the `parameters` cell, and must not also appear under `hyperparameters`. "
        "The value here replaces the notebook's own value in every run."
    ),
}

SECTION_ORDER = ["version", "application", "bridge", "store", "tuner", "runner", "job"]


# --------------------------------------------------------------- introspection


@dataclass(frozen=True)
class FieldDoc:
    path: str
    type_text: str
    default_text: str
    entry: Entry
    is_section: bool


def _unwrap_optional(annotation):
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_model(annotation) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _scalar_type(annotation) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of " + ", ".join(f"`{v}`" for v in get_args(annotation))
    if origin in (Union, types.UnionType):
        names = {_scalar_type(a) for a in get_args(annotation) if a is not type(None)}
        return "number" if names == {"integer", "number"} else " or ".join(sorted(names))
    if origin is list:
        return f"list of {_scalar_type(get_args(annotation)[0])}s"
    if origin is dict:
        return "mapping"
    return {int: "integer", float: "number", str: "text", bool: "true or false", Any: "any value"}.get(
        annotation, getattr(annotation, "__name__", str(annotation))
    )


def _constraints(field) -> str:
    parts = []
    for meta in field.metadata:
        for attr, text in (("ge", "at least {}"), ("gt", "greater than {}"), ("le", "at most {}"), ("lt", "less than {}")):
            value = getattr(meta, attr, None)
            if value is not None:
                parts.append(text.format(value))
        if getattr(meta, "min_length", None) is not None:
            n = meta.min_length
            parts.append("not empty" if n == 1 else f"at least {n} items")
    return ", ".join(parts)


def _default_text(field) -> str:
    if field.is_required():
        return "required"
    if field.default_factory is not None:
        default = field.default_factory()
        if isinstance(default, BaseModel):
            return "section is optional"
        if isinstance(default, list):
            return "one local MQTT connector"
        return "`{}`" if default == {} else f"`{default!r}`"
    default = field.default
    if default is None:
        return "none"
    if isinstance(default, bool):
        return "`true`" if default else "`false`"
    if isinstance(default, str):
        return f"`{default}`" if default else "empty text"
    return f"`{default}`"


def _walk(model: type[BaseModel], prefix: str, out: list[FieldDoc]) -> None:
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        annotation, _ = _unwrap_optional(field.annotation)
        origin = get_origin(annotation)

        child = None
        child_prefix = ""
        if _is_model(annotation):
            child, child_prefix = annotation, f"{path}."
        elif origin is list and _is_model(get_args(annotation)[0]):
            child, child_prefix, path = get_args(annotation)[0], f"{path}[].", path
        elif origin is dict and _is_model(get_args(annotation)[1]):
            child, child_prefix = get_args(annotation)[1], f"{path}.<name>."

        if child is not None and origin is list:
            type_text = "list of connections"
        elif child is not None and origin is dict:
            type_text = "mapping of name to range"
        elif child is not None:
            type_text = "section"
        else:
            type_text = _scalar_type(annotation)
        extra = _constraints(field)
        if extra:
            type_text += f", {extra}"

        entry_path = path
        out.append(FieldDoc(entry_path, type_text, _default_text(field), DESCRIPTIONS.get(entry_path, Entry("")), child is not None))
        if child is not None:
            if origin is dict:
                out.append(
                    FieldDoc(f"{path}.<name>", "section", "required", DESCRIPTIONS.get(f"{path}.<name>", Entry("")), True)
                )
            _walk(child, child_prefix, out)


def fields() -> list[FieldDoc]:
    """Every key of ``experiment.yaml``, in schema order, with its type, default and description."""
    out: list[FieldDoc] = []
    _walk(ExperimentSpec, "", out)
    return out


# -------------------------------------------------------------------- rendering


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _what_it_does(doc: FieldDoc) -> str:
    prefix = {RESERVED: "**Not used yet.** ", LABEL: "**Label only.** "}.get(doc.entry.status, "")
    return prefix + doc.entry.meaning


def render_markdown() -> str:
    """The key-by-key reference as Notion-compatible Markdown (pipe tables)."""
    docs = fields()
    by_section: dict[str, list[FieldDoc]] = {s: [] for s in SECTION_ORDER}
    for doc in docs:
        by_section[doc.path.split(".")[0].split("[")[0]].append(doc)

    lines: list[str] = []
    for section in SECTION_ORDER:
        rows = by_section[section]
        lines.append(f"## `{section}`")
        lines.append("")
        lines.append("| Key | Type | Default | What it does |")
        lines.append("|---|---|---|---|")
        for doc in rows:
            default = doc.entry.required or doc.default_text
            lines.append(f"| `{_cell(doc.path)}` | {_cell(doc.type_text)} | {_cell(default)} | {_cell(_what_it_does(doc))} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    print(render_markdown(), end="")