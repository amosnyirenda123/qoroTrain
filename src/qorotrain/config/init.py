"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/init.py

Description:
    Generate a starting ``experiment.yaml`` from a notebook.

    The ranges are guesses made from each parameter's current value. They are meant
    to be reviewed, so every guessed line carries a ``review`` comment.

    This module backs ``qorotrainctl init``. It reads the notebook's
    ``parameters`` cell statically (:mod:`qorotrain.config.notebook`),
    classifies each assignment as tunable or fixed with :func:`classify`, and
    renders a commented YAML document. The output deliberately contains
    ``metric: TODO`` so that :func:`~qorotrain.config.validate.validate_experiment`
    refuses to run it until a person has chosen the objective.

    Design:
        The YAML is produced from a text template rather than with
        ``yaml.dump`` so that it can carry explanatory comments and keep a
        stable, readable key order.

===============================================================================
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .notebook import PARAMETERS_TAG, read_notebook

# Name tokens that usually mean "budget or identifier", not something to tune.
_FIXED_TOKENS = {
    "seed", "episode", "episodes", "epoch", "epochs", "plays", "timesteps",
    "path", "dir", "file", "name", "device", "verbose", "debug", "render",
    "workers", "jobs",
    # sizes of the problem itself, not knobs of the training
    "arms", "classes", "features", "actions", "states",
}

# Name tokens that mean "a probability or a discount factor": the upper bound is 1.
_PROBABILITY_TOKENS = {"epsilon", "eps", "prob", "probability", "dropout", "gamma", "discount"}


class InitError(Exception):
    """A starting file cannot be generated from this notebook."""


@dataclass
class Guess:
    """How :func:`classify` proposes to treat one notebook parameter.

    Attributes:
        kind (str): ``"float"`` or ``"int"`` for a tunable range, ``"fixed"``
            to pass the value through unchanged.
        min (float | int | None): Proposed lower bound (tunable kinds only).
        max (float | int | None): Proposed upper bound (tunable kinds only).
        log (bool): Whether the range should be searched on a log scale.
        reason (str): Why a parameter was kept fixed; written as a YAML
            comment.
    """

    kind: str  # "float" | "int" | "fixed"
    min: float | int | None = None
    max: float | int | None = None
    log: bool = False
    reason: str = ""


@dataclass
class InitResult:
    """Output of :func:`generate_experiment`.

    Attributes:
        yaml (str): The generated ``experiment.yaml`` text.
        tuned (dict[str, Guess]): Parameters placed under
            ``job.hyperparameters``, with their guessed ranges.
        fixed (dict[str, Any]): Parameters placed under ``job.fixed``, with
            the notebook's values.
        problems (list[str]): Lines of the ``parameters`` cell that are not
            plain literal assignments (``validate`` will report them as
            errors).
    """

    yaml: str
    tuned: dict[str, Guess] = field(default_factory=dict)
    fixed: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # parameters-cell problems


def _round(x: float) -> float:
    """Round to three significant digits, for readable guessed bounds."""
    return float(f"{x:.3g}")


def classify(name: str, value: Any) -> Guess:
    """Decide whether a parameter looks tunable, and propose a range if so.

    The heuristic, in order:

    1. Non-numbers and booleans are fixed.
    2. Names containing a "budget/identifier/problem size" token (split on
       ``_``, for example ``num_episodes`` or ``seed``) are fixed.
    3. Integers ``>= 2`` get ``[value // 2, value * 2]``; smaller ones are
       fixed.
    4. Non-positive floats are fixed (no log range is possible).
    5. Probability-like names at or above 1 are fixed.
    6. Floats in ``[0.8, 1)`` look like discount factors:
       ``[min(value, 0.9), 0.999]``.
    7. Other floats below 1 get one decade either side, capped at 1, on a log
       scale when below 0.1.
    8. Floats ``>= 1`` get one decade either side on a log scale.

    Args:
        name: Variable name in the ``parameters`` cell.
        value: Its literal value.

    Returns:
        The proposed treatment of the parameter.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Guess("fixed")
    tokens = set(name.lower().split("_"))
    if _FIXED_TOKENS & tokens:
        return Guess("fixed", reason="looks like a budget, an identifier or a problem size")

    if isinstance(value, int):
        if value < 2:
            return Guess("fixed", reason="too small to guess a range")
        return Guess("int", max(1, value // 2), value * 2)

    if value <= 0:
        return Guess("fixed", reason="cannot guess a range from a non-positive value")
    if value >= 1 and _PROBABILITY_TOKENS & tokens:
        return Guess("fixed", reason="a probability already at its upper bound")
    if 0.8 <= value < 1:  # discount-factor-like
        return Guess("float", _round(min(value, 0.9)), 0.999)
    if value < 1:
        return Guess("float", _round(value / 10), _round(min(value * 10, 1.0)), log=value < 0.1)
    return Guess("float", _round(value / 10), _round(value * 10), log=True)


def _num(x: float | int) -> str:
    """Format a bound so that YAML reads it back with the same type.

    Integers are written as is. Floats use ``repr``; an exponent without a
    decimal point (``1e-05``) gets one inserted (``1.0e-05``) so that strict
    YAML 1.1 parsers also read it as a float.
    """
    if isinstance(x, int):
        return str(x)
    text = repr(float(x))
    mantissa, _, exponent = text.partition("e")
    if exponent and "." not in mantissa:
        text = f"{mantissa}.0e{exponent}"
    return text


def _inline(value: Any) -> str:
    """Render a value as single-line YAML flow syntax (for ``job.fixed`` entries)."""
    return yaml.safe_dump(value, default_flow_style=True, width=10_000).replace("\n...\n", "").strip()


def _slug(stem: str) -> str:
    """Turn a notebook file stem into a topic-safe experiment name (``[a-z0-9-]``)."""
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "experiment"


def generate_experiment(notebook_path: str | Path, output_dir: str | Path = ".") -> InitResult:
    """Build a starting ``experiment.yaml`` for a notebook.

    Args:
        notebook_path: The notebook to read.
        output_dir: Directory the YAML will be written to; ``job.source`` is
            made relative to it (with ``/`` separators).

    Returns:
        The generated text and the classification of every parameter. Nothing
        is written to disk; the caller decides where the text goes.

    Raises:
        InitError: If no cell is tagged ``parameters`` or the tagged cell has
            no literal assignments.
        qorotrain.config.notebook.NotebookError: If the notebook cannot be
            read.
    """
    notebook = read_notebook(notebook_path)
    params = notebook.parameters
    if not params.found:
        raise InitError(
            f"no cell is tagged '{PARAMETERS_TAG}' in {Path(notebook_path).name} "
            "(JupyterLab: select the cell, open the right sidebar, Common Tools, Cell Tags)"
        )
    if not params.values:
        raise InitError(
            f"the '{PARAMETERS_TAG}' cell has no plain assignments to read; "
            "keep only lines like `learning_rate = 0.001` in it"
        )

    result = InitResult(yaml="", problems=list(params.problems))
    tuned_lines: list[str] = []
    fixed_lines: list[str] = []

    for name, value in params.values.items():
        guess = classify(name, value)
        if guess.kind == "fixed":
            result.fixed[name] = value
            note = f"  # review: kept fixed ({guess.reason})" if guess.reason else ""
            fixed_lines.append(f"    {name}: {_inline(value)}{note}")
        else:
            result.tuned[name] = guess
            log = ", log: true" if guess.log else ""
            tuned_lines.append(
                f"    {name}: {{type: {guess.kind}, min: {_num(guess.min)}, max: {_num(guess.max)}{log}}}"
                f"  # review: guessed from {value!r}"
            )

    source = os.path.relpath(Path(notebook_path), Path(output_dir)).replace(os.sep, "/")
    nb_name = Path(notebook_path).name
    hyper_block = "\n".join(tuned_lines) if tuned_lines else "    {}  # TODO: nothing looked tunable"
    fixed_block = "\n".join(fixed_lines) if fixed_lines else "    {}"

    result.yaml = f"""\
# Generated by `qorotrainctl init` from {nb_name}.
# Lines marked "review" are guesses made from the notebook's current values.
version: 1
application: {{name: {_slug(Path(notebook_path).stem)}}}

bridge:
  connectors:
    - {{type: mqtt, host: localhost, port: 1883}}

tuner:
  algorithm: random     # random | grid (the LLM agent and Optuna are not available yet)
  max_runs: 8           # counts the baseline run, which uses the notebook's own values
  seed: 0
  review: {{mode: auto}}  # manual review is not available yet

job:
  source: {source}
  objective:
    metric: TODO          # a metric your notebook logs, e.g. reward
    aggregate: last       # last | best | mean | mean_last_N
    direction: maximize   # review
  hyperparameters:        # what the tuner may change
{hyper_block}
  fixed:                  # passed to every run, never tuned
{fixed_block}
"""
    return result
