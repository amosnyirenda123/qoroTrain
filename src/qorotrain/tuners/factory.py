"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/factory.py

Description:
    Build a ready-to-attach Tuner from a loaded ``experiment.yaml``.

    This module is the seam between the declarative configuration
    (:class:`~qorotrain.config.models.ExperimentSpec`) and the tuning classes
    in :mod:`qorotrain.tuners.tuner`. The CLI's ``run`` command uses
    :func:`initial_from_notebook` to obtain the baseline parameters and
    :func:`create_tuner` to instantiate the configured algorithm.

    Error handling:
        Algorithms and review modes that the schema accepts but the code does
        not yet implement raise :class:`NotImplementedError` with a message
        listing what *is* available; the CLI turns that into exit code 2.

===============================================================================
"""
from pathlib import Path
from typing import Any

from ..config.models import ExperimentSpec
from ..config.notebook import read_notebook
from ..config.validate import resolve_source
from ..store.metric_store import MetricStore
from .review import Reviewer
from .space import SearchSpace
from .tuner import GridTuner, RandomTuner, Tuner


def initial_from_notebook(spec: ExperimentSpec, base_dir: str | Path = ".") -> dict[str, Any]:
    """The notebook's current values of the tunable parameters: the baseline run.

    The notebook is parsed statically (never executed): values are read from
    the literal assignments of its ``parameters`` cell.

    Args:
        spec: The loaded experiment.
        base_dir: Directory that ``job.source`` is relative to (the directory
            of ``experiment.yaml``).

    Returns:
        A mapping from each name in ``job.hyperparameters`` to the value the
        notebook assigns it, in declaration order.

    Raises:
        ValueError: If a tunable parameter is not assigned a literal value in
            the ``parameters`` cell.
        qorotrain.config.notebook.NotebookError: If the notebook cannot be
            read.
    """
    values = read_notebook(resolve_source(spec, base_dir)).parameters.values
    missing = [name for name in spec.job.hyperparameters if name not in values]
    if missing:
        raise ValueError(f"the notebook does not assign: {', '.join(missing)}")
    return {name: values[name] for name in spec.job.hyperparameters}


def create_tuner(
    spec: ExperimentSpec,
    store: MetricStore,
    *,
    initial: dict[str, Any] | None = None,
    reviewer: Reviewer | None = None,
    session: str | None = None,
    **overrides: Any,
) -> Tuner:
    """Build the tuner named by ``tuner.algorithm``. ``overrides`` reach the Tuner constructor.

    Args:
        spec: The loaded experiment.
        store: Store the tuner records runs and metrics in.
        initial: Baseline parameters for run 0, usually from
            :func:`initial_from_notebook`. ``None`` makes run 0 the algorithm's
            first proposal.
        reviewer: Reviewer for proposals. Required when
            ``tuner.review.mode`` is ``manual``; defaults to auto-approval.
        session: Prefix for run identifiers. Random when omitted.
        **overrides: Extra or replacement keyword arguments for the
            :class:`~qorotrain.tuners.tuner.Tuner` constructor (for example
            ``settle_seconds`` or ``max_failures`` in tests).

    Returns:
        A :class:`~qorotrain.tuners.tuner.RandomTuner` or
        :class:`~qorotrain.tuners.tuner.GridTuner`, not yet attached to a
        Bridge.

    Raises:
        NotImplementedError: If manual review is configured without a
            ``reviewer``, or the algorithm is ``agent`` or ``optuna``.
        ValueError: For ``grid``, if the grid is larger than
            :data:`~qorotrain.tuners.space.MAX_GRID_SIZE`.
    """
    tuner = spec.tuner
    if tuner.review.mode == "manual" and reviewer is None:
        raise NotImplementedError(
            "manual review is not available yet: set tuner.review.mode to 'auto', or pass a reviewer"
        )

    common = dict(
        space=SearchSpace.from_spec(spec.job),
        objective=spec.job.objective,
        project=spec.application.name,
        store=store,
        topic_root=spec.bridge.topic_root,
        max_runs=tuner.max_runs,
        stop=tuner.stop,
        initial=initial,
        reviewer=reviewer,
        session=session,
    )
    common.update(overrides)

    match tuner.algorithm:
        case "random":
            return RandomTuner(seed=tuner.seed, **common)
        case "grid":
            return GridTuner(points=tuner.grid_points, seed=tuner.seed, **common)
        case other:
            raise NotImplementedError(f"tuner.algorithm '{other}' is not available yet (available: random, grid)")
