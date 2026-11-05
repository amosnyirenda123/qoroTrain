import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import ExperimentSpec

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# PyYAML follows YAML 1.1, where ``1e-5`` (no dot) is a *string*. People write
# learning rates like that all the time, so accept it as a float.
_FLOAT = re.compile(
    r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
        |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
        |\.[0-9_]+(?:[eE][-+][0-9]+)?
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
    re.X,
)


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


_Loader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT, list("-+0123456789."))


class ConfigError(Exception):
    """experiment.yaml is missing, malformed, or fails validation."""

    def __init__(self, problems: list[str], source: str = "experiment") -> None:
        self.problems = problems
        self.source = source
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{len(problems)} problem(s) in {source}:\n{lines}")


def _substitute(value: Any, path: str, missing: list[str]) -> Any:
    """Replace ``${NAME}`` in every string with the environment variable NAME."""
    if isinstance(value, str):

        def repl(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                missing.append(f"{path}: environment variable {name} is not set")
                return match.group(0)
            return os.environ[name]

        return _ENV.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, f"{path}.{k}" if path else str(k), missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, f"{path}[{i}]", missing) for i, v in enumerate(value)]
    return value


def _format_errors(exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        elif err["type"] == "missing":
            msg = "required"
        else:
            msg = err["msg"].removeprefix("Value error, ")
        problems.append(f"{loc}: {msg}")
    return problems


def parse_experiment(text: str, source: str = "experiment") -> ExperimentSpec:
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ConfigError([f"invalid YAML: {exc}"], source) from exc
    if not isinstance(data, dict):
        raise ConfigError(["the file must contain a mapping at the top level"], source)

    missing: list[str] = []
    data = _substitute(data, "", missing)
    if missing:
        raise ConfigError(missing, source)

    try:
        return ExperimentSpec.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc), source) from exc


def load_experiment(path: str | Path) -> ExperimentSpec:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read file: {exc}"], str(path)) from exc
    return parse_experiment(text, str(path))