"""Schema of ``experiment.yaml``. Unknown keys are rejected everywhere."""
import keyword
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..connectors.config import MQTTConfig

Number = int | float


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")




class ApplicationSpec(_Model):
    name: str = Field(min_length=1)
    log_level: Literal["info", "debug"] = "info"


class ConnectorSpec(_Model):
    name: str | None = None
    type: Literal["mqtt"] = "mqtt"
    host: str = "localhost"
    port: int = Field(1883, ge=1, le=65535)
    client_id: str = ""  # empty -> generated
    username: str | None = None
    password: str | None = None
    default_qos: Literal[0, 1, 2] = 1

    def to_config(self) -> MQTTConfig:
        return MQTTConfig(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            qos=self.default_qos,
            username=self.username,
            password=self.password,
        )


class BridgeSpec(_Model):
    topic_root: str = Field("tuning", min_length=1)
    connectors: list[ConnectorSpec] = Field(default_factory=lambda: [ConnectorSpec()], min_length=1)

    @model_validator(mode="after")
    def _unique_names(self):
        names = [c.name for c in self.connectors if c.name is not None]
        if len(names) != len(set(names)):
            raise ValueError("connector names must be unique")
        return self


class StoreSpec(_Model):
    path: str = "./qorotrain.db"



class StopSpec(_Model):
    target: float | None = None  # stop when the objective reaches this value
    patience: int | None = Field(None, ge=1)  # stop after this many runs without improvement


class ReviewSpec(_Model):
    mode: Literal["manual", "auto"] = "manual"


class EmaSpec(_Model):
    fast: float = Field(0.1, gt=0, le=1)
    slow: float = Field(0.01, gt=0, le=1)

    @model_validator(mode="after")
    def _fast_above_slow(self):
        if self.fast <= self.slow:
            raise ValueError("ema.fast must be larger than ema.slow")
        return self


class PlateauSpec(_Model):
    window: int = Field(50, ge=2)
    epsilon: float = Field(0.01, gt=0)


class SummarizerSpec(_Model):
    flush_every: int = Field(200, ge=1)
    flush_seconds: float = Field(2.0, gt=0)
    ema: EmaSpec = Field(default_factory=EmaSpec)
    plateau: PlateauSpec = Field(default_factory=PlateauSpec)


class LLMSpec(_Model):
    provider: str = "ollama"
    model: str = Field(min_length=1)
    temperature: float = Field(0.2, ge=0, le=2)


class AgentSpec(_Model):
    llm: LLMSpec
    use_notebook_context: bool = True
    retrieval: bool = False


class TunerSpec(_Model):
    algorithm: Literal["agent", "random", "optuna"]
    max_runs: int = Field(8, ge=1)
    stop: StopSpec = Field(default_factory=StopSpec)
    review: ReviewSpec = Field(default_factory=ReviewSpec)
    summarizer: SummarizerSpec = Field(default_factory=SummarizerSpec)
    agent: AgentSpec | None = None

    @model_validator(mode="after")
    def _agent_needs_an_llm(self):
        if self.algorithm == "agent" and self.agent is None:
            raise ValueError("tuner.agent (with an llm) is required when algorithm is 'agent'")
        return self





class RunnerSpec(_Model):
    workdir: str | None = None
    kernel: str = "python3"
    timeout: int = Field(3600, ge=1)  # seconds, applies per notebook cell
    output_dir: str = "./runs"





class ObjectiveSpec(_Model):
    metric: str = Field(min_length=1)
    # how streamed values become one score
    aggregate: str = Field("last", pattern=r"^(last|best|mean|mean_last_[1-9][0-9]*)$")
    direction: Literal["maximize", "minimize"]


class HyperParamSpec(_Model):
    type: Literal["float", "int"]
    min: Number | None = None
    max: Number | None = None
    choices: list[Number] | None = Field(None, min_length=2)
    log: bool = False

    @model_validator(mode="after")
    def _check(self):
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
        """None if ``value`` fits this spec, otherwise a short reason why not."""
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
    name: str | None = None
    source: str = Field(min_length=1)  # path to the notebook, relative to this file
    objective: ObjectiveSpec
    hyperparameters: dict[str, HyperParamSpec] = Field(min_length=1)
    fixed: dict[str, Any] = Field(default_factory=dict)

    @field_validator("hyperparameters", "fixed")
    @classmethod
    def _names_are_variables(cls, value):
        for name in value:
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"{name!r} is not a valid Python variable name")
        return value

    @model_validator(mode="after")
    def _no_overlap(self):
        both = sorted(set(self.hyperparameters) & set(self.fixed))
        if both:
            raise ValueError(f"declared as both tuned and fixed: {', '.join(both)}")
        return self





class ExperimentSpec(_Model):
    version: Literal[1]
    application: ApplicationSpec
    bridge: BridgeSpec = Field(default_factory=BridgeSpec)
    store: StoreSpec = Field(default_factory=StoreSpec)
    tuner: TunerSpec
    runner: RunnerSpec = Field(default_factory=RunnerSpec)
    job: JobSpec