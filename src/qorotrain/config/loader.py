"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/loader.py

Description:
    Load ``experiment.yaml`` into a validated
    :class:`~qorotrain.config.models.ExperimentSpec`.

    Loading is a three-stage pipeline, and every stage reports *all* of its
    problems at once through a single exception type, :class:`ConfigError`:

    1. **Parse** with a stricter YAML loader (``_Loader``) that rejects
       duplicate keys and accepts exponent floats such as ``1e-5``.
    2. **Substitute** ``${NAME}`` references with environment variables, so
       secrets such as broker passwords stay out of the file.
    3. **Validate** against the Pydantic schema, translating Pydantic's error
       objects into short ``path: message`` lines.

    Design:
        Only this module knows about PyYAML and Pydantic error formats; the
        CLI only ever catches :class:`ConfigError` and prints it.

===============================================================================
"""
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
    """``yaml.SafeLoader`` that rejects duplicate mapping keys.

    Standard PyYAML silently keeps the last of two identical keys, which would
    hide a typo such as declaring ``learning_rate`` twice. The class also gets
    an extended implicit float resolver (registered below) so that YAML 1.2
    style exponents like ``1e-5`` load as floats.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Construct a mapping, failing on the first repeated key.

        Args:
            node: The YAML mapping node.
            deep: Passed through to ``SafeLoader.construct_mapping``.

        Returns:
            The constructed mapping.

        Raises:
            yaml.constructor.ConstructorError: If a key occurs twice; the
                error carries the position of the duplicate.
        """
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
    """experiment.yaml is missing, malformed, or fails validation.

    The string form is a ready-to-print report::

        2 problem(s) in experiment.yaml:
          - tuner.algorithm: required
          - tuner.max_run: unknown key

    Attributes:
        problems (list[str]): One line per problem, ``path: message``.
        source (str): Name of the file (or other source) that was loaded.
    """

    def __init__(self, problems: list[str], source: str = "experiment") -> None:
        """Create the error and format its message.

        Args:
            problems: Problem descriptions.
            source: Name of the configuration source, used in the message.
        """
        self.problems = problems
        self.source = source
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{len(problems)} problem(s) in {source}:\n{lines}")


def _substitute(value: Any, path: str, missing: list[str]) -> Any:
    """Replace ``${NAME}`` in every string with the environment variable NAME.

    Walks dictionaries and lists recursively. Unset variables are left in
    place and reported in ``missing`` (with the key path) so that all of them
    can be reported together.

    Args:
        value: Parsed YAML value (any nesting of dict, list and scalars).
        path: Dotted key path of ``value``, for error messages.
        missing: Accumulator for "variable is not set" problems; mutated.

    Returns:
        A copy of ``value`` with substitutions applied. Non-string scalars
        are returned unchanged.
    """
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
    """Translate a Pydantic validation error into ``path: message`` lines.

    Pydantic's "extra fields not permitted" and "field required" messages are
    shortened to ``unknown key`` and ``required``; the ``Value error, ``
    prefix that Pydantic adds to custom validator messages is removed.

    Args:
        exc: The validation error raised by ``ExperimentSpec.model_validate``.

    Returns:
        One line per individual error.
    """
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
    """Parse and validate the text of an ``experiment.yaml``.

    Args:
        text: YAML document.
        source: Name used in error messages.

    Returns:
        The validated experiment specification.

    Raises:
        ConfigError: If the YAML is invalid or has duplicate keys, the top
            level is not a mapping, an environment variable is missing, or the
            schema validation fails.
    """
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
    """Read, parse and validate an ``experiment.yaml`` file.

    Args:
        path: Path of the file, read as UTF-8.

    Returns:
        The validated experiment specification.

    Raises:
        ConfigError: If the file cannot be read, or for any reason listed in
            :func:`parse_experiment`.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read file: {exc}"], str(path)) from exc
    return parse_experiment(text, str(path))
