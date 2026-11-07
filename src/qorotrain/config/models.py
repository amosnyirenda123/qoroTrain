"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/models.py

Description:
    Schema of ``experiment.yaml``. Unknown keys are rejected everywhere.

    The schema is a tree of Pydantic v2 models rooted at
    :class:`ExperimentSpec`, one model per YAML section. It is the single
    source of truth for keys, types, defaults and constraints:
    :mod:`qorotrain.config.loader` validates against it,
    :mod:`qorotrain.config.reference` introspects it to generate the
    reference page, and the runtime components read their settings from the
    validated objects.

    Design:
        Every model derives from a private base with ``extra="forbid"``, so a
        misspelt key is an error instead of being silently ignored.
        Cross-field rules (``min < max``, unique connector names, an LLM
        section when the algorithm is ``agent``...) are implemented as
        ``model_validator(mode="after")`` hooks whose ``ValueError`` messages
        surface verbatim in :class:`~qorotrain.config.loader.ConfigError`.

    Sections accepted but not yet used at runtime:
        :class:`SummarizerSpec` (with :class:`EmaSpec` and
        :class:`PlateauSpec`) and :class:`AgentSpec` (with :class:`LLMSpec`)
        are validated but nothing reads them yet.

===============================================================================
"""
import keyword
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..connectors.config import MQTTConfig

Number = int | float
"""A YAML number, either an integer or a float."""


class _Model(BaseModel):
    """Base of every schema model: unknown keys are a validation error."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- application


class ApplicationSpec(_Model):
    """The ``application`` section: identity of the experiment.

    Attributes:
        name (str): Experiment name; the ``<project>`` segment of every topic
            and the ``project`` column of every stored run. Must be non-empty.
        log_level (Literal["info", "debug"]): Verbosity of
            ``qorotrainctl run`` on stderr.
    """

    name: str = Field(min_length=1)
    log_level: Literal["info", "debug"] = "info"


# --------------------------------------------------------------------- bridge


class ConnectorSpec(_Model):
    """One entry of ``bridge.connectors``: a broker connection.

    Attributes:
        name (str | None): Label used in messages only.
        type (Literal["mqtt"]): Broker kind; only MQTT exists.
        host (str): Broker host.
        port (int): Broker TCP port, 1 to 65535.
        client_id (str): Fixed MQTT client id; empty means generated.
        username (str | None): Broker username.
        password (str | None): Broker password (use ``${VAR}``
            substitution).
        default_qos (Literal[0, 1, 2]): Fallback QoS for publish/subscribe.
    """

    name: str | None = None
    type: Literal["mqtt"] = "mqtt"
    host: str = "localhost"
    port: int = Field(1883, ge=1, le=65535)
    client_id: str = ""  # empty -> generated
    username: str | None = None
    password: str | None = None
    default_qos: Literal[0, 1, 2] = 1

    def to_config(self) -> MQTTConfig:
        """Convert to the runtime connector configuration.

        Returns:
            An :class:`~qorotrain.connectors.config.MQTTConfig` with the same
            settings (``default_qos`` becomes ``qos``).
        """
        return MQTTConfig(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            qos=self.default_qos,
            username=self.username,
            password=self.password,
        )


class BridgeSpec(_Model):
    """The ``bridge`` section: messaging setup.

    Attributes:
        topic_root (str): First segment of every topic.
        connectors (list[ConnectorSpec]): At least one broker connection;
            defaults to a single MQTT connector on ``localhost:1883``. The
            first entry is used for publishing and is handed to the notebook.

    Raises:
        ValueError: If two connectors have the same ``name``.
    """

    topic_root: str = Field("tuning", min_length=1)
    connectors: list[ConnectorSpec] = Field(default_factory=lambda: [ConnectorSpec()], min_length=1)

    @model_validator(mode="after")
    def _unique_names(self):
        """Reject duplicate connector names (unnamed connectors are not compared)."""
        names = [c.name for c in self.connectors if c.name is not None]
        if len(names) != len(set(names)):
            raise ValueError("connector names must be unique")
        return self


class StoreSpec(_Model):
    """The ``store`` section: where results are persisted.

    Attributes:
        path (str): SQLite file for runs and metrics, relative to
            ``experiment.yaml``. ``bridge.db`` is created next to it.
    """

    path: str = "./qorotrain.db"


# ---------------------------------------------------------------------- tuner


class StopSpec(_Model):
    """The ``tuner.stop`` section: early-stopping rules on top of ``max_runs``.

    Attributes:
        target (float | None): Stop once the best objective reaches this
            value (``>=`` when maximizing, ``<=`` when minimizing).
        patience (int | None): Stop after this many consecutive runs without
            a new best objective; at least 1.
    """

    target: float | None = None  # stop when the objective reaches this value
    patience: int | None = Field(None, ge=1)  # stop after this many runs without improvement


class ReviewSpec(_Model):
    """The ``tuner.review`` section.

    Attributes:
        mode (Literal["manual", "auto"]): ``auto`` approves every proposal;
            ``manual`` (the default) is not implemented yet, so
            :func:`~qorotrain.tuners.factory.create_tuner` rejects it unless a
            reviewer is supplied in code.
    """

    mode: Literal["manual", "auto"] = "manual"


class EmaSpec(_Model):
    """Smoothing factors of the two exponential moving averages (reserved, not used yet).

    Attributes:
        fast (float): Smoothing factor of the fast average, in ``(0, 1]``.
        slow (float): Smoothing factor of the slow average, in ``(0, 1]``.

    Raises:
        ValueError: If ``fast <= slow``.
    """

    fast: float = Field(0.1, gt=0, le=1)
    slow: float = Field(0.01, gt=0, le=1)

    @model_validator(mode="after")
    def _fast_above_slow(self):
        """Require the fast average to react faster than the slow one."""
        if self.fast <= self.slow:
            raise ValueError("ema.fast must be larger than ema.slow")
        return self


class PlateauSpec(_Model):
    """Plateau detection settings (reserved, not used yet).

    Attributes:
        window (int): Number of recent points examined; at least 2.
        epsilon (float): Relative improvement below which a metric counts as
            plateaued; greater than 0.
    """

    window: int = Field(50, ge=2)
    epsilon: float = Field(0.01, gt=0)


class SummarizerSpec(_Model):
    """The ``tuner.summarizer`` section (reserved, not used yet).

    Attributes:
        flush_every (int): Points to collect before updating the summary.
        flush_seconds (float): Longest wait, in seconds, before updating.
        ema (EmaSpec): Trend estimation settings.
        plateau (PlateauSpec): Plateau detection settings.
    """

    flush_every: int = Field(200, ge=1)
    flush_seconds: float = Field(2.0, gt=0)
    ema: EmaSpec = Field(default_factory=EmaSpec)
    plateau: PlateauSpec = Field(default_factory=PlateauSpec)


class LLMSpec(_Model):
    """Language model used by the agent (reserved, not used yet).

    Attributes:
        provider (str): Where the model runs, for example ``ollama``.
        model (str): Model name; required and non-empty.
        temperature (float): Sampling temperature, 0 to 2.
    """

    provider: str = "ollama"
    model: str = Field(min_length=1)
    temperature: float = Field(0.2, ge=0, le=2)


class AgentSpec(_Model):
    """The ``tuner.agent`` section (reserved, not used yet).

    Attributes:
        llm (LLMSpec): Language model settings; required.
        use_notebook_context (bool): Include notebook code in the prompt.
        retrieval (bool): Retrieve similar past trials.
    """

    llm: LLMSpec
    use_notebook_context: bool = True
    retrieval: bool = False


class TunerSpec(_Model):
    """The ``tuner`` section: search algorithm, budget and stop rules.

    Attributes:
        algorithm (Literal["agent", "random", "grid", "optuna"]): Search
            algorithm. Only ``random`` and ``grid`` are implemented.
        max_runs (int): Run budget including the baseline run; at least 1.
        seed (int): Random seed for ``random`` and ``grid``.
        baseline (bool): Whether run 0 uses the notebook's own values.
        grid_points (int): Values per numeric range for ``grid``; at least 2.
        stop (StopSpec): Early-stopping rules.
        review (ReviewSpec): Review mode.
        summarizer (SummarizerSpec): Reserved.
        agent (AgentSpec | None): Required when ``algorithm`` is ``agent``.

    Raises:
        ValueError: If ``algorithm`` is ``agent`` and ``agent`` is missing.
    """

    algorithm: Literal["agent", "random", "grid", "optuna"]
    max_runs: int = Field(8, ge=1)  # counts the baseline run too
    seed: int = 0  # random and grid: makes a session repeatable
    baseline: bool = True  # run 0 uses the notebook's own values
    grid_points: int = Field(3, ge=2)  # grid: values per range, interpolated between min and max
    stop: StopSpec = Field(default_factory=StopSpec)
    review: ReviewSpec = Field(default_factory=ReviewSpec)
    summarizer: SummarizerSpec = Field(default_factory=SummarizerSpec)
    agent: AgentSpec | None = None

    @model_validator(mode="after")
    def _agent_needs_an_llm(self):
        """Require the agent section when the agent algorithm is selected."""
        if self.algorithm == "agent" and self.agent is None:
            raise ValueError("tuner.agent (with an llm) is required when algorithm is 'agent'")
        return self


# --------------------------------------------------------------------- runner


class RunnerSpec(_Model):
    """The ``runner`` section: how the notebook is executed by Papermill.

    Attributes:
        workdir (str | None): Kernel working directory (Papermill ``--cwd``),
            relative to ``experiment.yaml``.
        kernel (str): Jupyter kernel name (Papermill ``-k``).
        timeout (int): Per-cell execution timeout in seconds (Papermill
            ``--execution-timeout``); at least 1.
        output_dir (str): Folder for per-run artefacts, relative to
            ``experiment.yaml``.
    """

    workdir: str | None = None
    kernel: str = "python3"
    timeout: int = Field(3600, ge=1)  # seconds, applies per notebook cell
    output_dir: str = "./runs"


# ------------------------------------------------------------------------ job


class ObjectiveSpec(_Model):
    """The ``job.objective`` section: how a run is scored.

    Attributes:
        metric (str): Name of the logged metric that defines success.
        aggregate (str): Reduction of the metric stream to one score:
            ``last``, ``best``, ``mean`` or ``mean_last_N`` (N >= 1), enforced
            by a regular expression. See
            :func:`~qorotrain.tuners.objective.aggregate`.
        direction (Literal["maximize", "minimize"]): Whether higher or lower
            scores are better.
    """

    metric: str = Field(min_length=1)
    # how streamed values become one score
    aggregate: str = Field("last", pattern=r"^(last|best|mean|mean_last_[1-9][0-9]*)$")
    direction: Literal["maximize", "minimize"]


class HyperParamSpec(_Model):
    """Search range of one tunable parameter (an entry of ``job.hyperparameters``).

    Exactly one of two forms is allowed: a range (``min`` and ``max``,
    optionally ``log``) or an explicit list of ``choices``.

    Attributes:
        type (Literal["float", "int"]): Value type of proposals.
        min (Number | None): Inclusive lower bound of a range.
        max (Number | None): Inclusive upper bound of a range.
        choices (list[Number] | None): Explicit allowed values, at least two.
        log (bool): Sample and interpolate the range on a log scale.

    Raises:
        ValueError: If both forms or neither form are given, ``min >= max``,
            ``log`` is used with ``choices`` or with ``min <= 0``, or an
            ``int`` parameter has non-whole bounds or choices.
    """

    type: Literal["float", "int"]
    min: Number | None = None
    max: Number | None = None
    choices: list[Number] | None = Field(None, min_length=2)
    log: bool = False

    @model_validator(mode="after")
    def _check(self):
        """Enforce the range/choices rules listed in the class docstring."""
        has_range = self.min is not None or self.max is not None
        if self.choices is not None:
            if has_range:
                raise ValueError("use either choices or min/max, not both")
            if self.log:
                raise ValueError("log only applies to min/max ranges")
            bounds: list[Number] = list(self.choices)
        else:
            if self.min is None or self.max is None:
                raise ValueError("a range needs both min and max (or use choices)")
            if self.min >= self.max:
                raise ValueError("min must be less than max")
            if self.log and self.min <= 0:
                raise ValueError("log scale needs min > 0")
            bounds = [self.min, self.max]
        if self.type == "int" and any(float(b) != int(b) for b in bounds):
            raise ValueError("an int parameter needs whole-number bounds")
        return self

    def accepts(self, value: Any) -> str | None:
        """None if ``value`` fits this spec, otherwise a short reason why not.

        Booleans are rejected even though ``bool`` subclasses ``int``. For an
        ``int`` parameter a float is accepted only if it is a whole number
        (``64.0``).

        Args:
            value: Candidate value.

        Returns:
            ``None`` if the value is acceptable, otherwise a reason suitable
            for an error message.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"value {value!r} is not a number"
        if self.type == "int" and isinstance(value, float) and not value.is_integer():
            return f"value {value!r} is not a whole number but the parameter is int"
        if self.choices is not None:
            return None if value in self.choices else f"value {value!r} is not one of {self.choices}"
        if not (self.min <= value <= self.max):
            return f"value {value!r} is outside the range [{self.min:g}, {self.max:g}]"
        return None


class JobSpec(_Model):
    """The ``job`` section: what is being tuned.

    Attributes:
        name (str | None): Optional label; not read by the code.
        source (str): Notebook path, relative to ``experiment.yaml``.
        objective (ObjectiveSpec): How runs are scored.
        hyperparameters (dict[str, HyperParamSpec]): Tunable parameters, at
            least one; names must be valid Python identifiers.
        fixed (dict[str, Any]): Values passed to every run and never tuned;
            names must be valid Python identifiers.

    Raises:
        ValueError: If a name is not a valid, non-keyword identifier, or a
            name appears in both ``hyperparameters`` and ``fixed``.
    """

    name: str | None = None
    source: str = Field(min_length=1)  # path to the notebook, relative to this file
    objective: ObjectiveSpec
    hyperparameters: dict[str, HyperParamSpec] = Field(min_length=1)
    fixed: dict[str, Any] = Field(default_factory=dict)

    @field_validator("hyperparameters", "fixed")
    @classmethod
    def _names_are_variables(cls, value):
        """Require every key to be a Python variable name (it is assigned in the notebook)."""
        for name in value:
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"{name!r} is not a valid Python variable name")
        return value

    @model_validator(mode="after")
    def _no_overlap(self):
        """Reject a parameter declared both as tuned and as fixed."""
        both = sorted(set(self.hyperparameters) & set(self.fixed))
        if both:
            raise ValueError(f"declared as both tuned and fixed: {', '.join(both)}")
        return self


# ----------------------------------------------------------------- experiment


class ExperimentSpec(_Model):
    """Root of ``experiment.yaml``.

    Attributes:
        version (Literal[1]): Schema version; must be ``1``.
        application (ApplicationSpec): Experiment identity; required.
        bridge (BridgeSpec): Messaging setup; optional.
        store (StoreSpec): Persistence settings; optional.
        tuner (TunerSpec): Search settings; required.
        runner (RunnerSpec): Execution settings; optional.
        job (JobSpec): The notebook and its parameters; required.
    """

    version: Literal[1]
    application: ApplicationSpec
    bridge: BridgeSpec = Field(default_factory=BridgeSpec)
    store: StoreSpec = Field(default_factory=StoreSpec)
    tuner: TunerSpec
    runner: RunnerSpec = Field(default_factory=RunnerSpec)
    job: JobSpec
