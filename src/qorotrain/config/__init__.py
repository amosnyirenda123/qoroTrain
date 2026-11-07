"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/config/__init__.py

Description:
    Configuration subsystem: everything that happens to ``experiment.yaml``
    and the notebook *before* anything runs.

    This package is a facade. It re-exports the names the CLI and library
    users need, so they can write ``from qorotrain.config import
    load_experiment`` without knowing the internal module layout:

    - :mod:`~qorotrain.config.models`: the Pydantic schema
      (:class:`~qorotrain.config.models.ExperimentSpec`).
    - :mod:`~qorotrain.config.loader`: YAML parsing, ``${ENV}`` substitution
      and schema validation (:func:`~qorotrain.config.loader.load_experiment`,
      :class:`~qorotrain.config.loader.ConfigError`).
    - :mod:`~qorotrain.config.validate`: cross-checks against the notebook and
      the broker (:func:`~qorotrain.config.validate.validate_experiment`).
    - :mod:`~qorotrain.config.notebook`: static notebook inspection
      (:func:`~qorotrain.config.notebook.read_notebook`).
    - :mod:`~qorotrain.config.init`: generation of a starting
      ``experiment.yaml`` (:func:`~qorotrain.config.init.generate_experiment`).
    - :mod:`~qorotrain.config.reference`: the key-by-key reference page.
    - :mod:`~qorotrain.config.environment` and
      :mod:`~qorotrain.config.contracts.schema`: the runtime contracts between
      processes (environment variables and message payloads).

===============================================================================
"""
from .init import InitError, InitResult, generate_experiment
from .loader import ConfigError, load_experiment, parse_experiment
from .models import ExperimentSpec
from .notebook import NotebookError, read_notebook
from .validate import ValidationReport, resolve_source, validate_experiment

__all__ = [
    "ConfigError",
    "ExperimentSpec",
    "InitError",
    "InitResult",
    "NotebookError",
    "ValidationReport",
    "generate_experiment",
    "load_experiment",
    "parse_experiment",
    "read_notebook",
    "resolve_source",
    "validate_experiment",
]
