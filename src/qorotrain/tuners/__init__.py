"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/__init__.py

Description:
    Tuning subsystem: the loop that decides which configuration a notebook
    runs next.

    This package re-exports the public names of its modules so callers can
    write ``from qorotrain.tuners import RandomTuner``:

    - :mod:`~qorotrain.tuners.tuner`: the :class:`~qorotrain.tuners.tuner.Tuner` base class (owns
      the run loop) and the algorithms :class:`~qorotrain.tuners.tuner.RandomTuner`,
      :class:`~qorotrain.tuners.tuner.GridTuner`, plus the not-yet-implemented
      :class:`~qorotrain.tuners.tuner.AgenticTuner` and
      :class:`~qorotrain.tuners.tuner.OptunaTuner`.
    - :mod:`~qorotrain.tuners.space`: :class:`~qorotrain.tuners.space.SearchSpace`, sampling and grid
      construction over the declared hyperparameters.
    - :mod:`~qorotrain.tuners.review`: the human-in-the-loop hook
      (:class:`~qorotrain.tuners.review.Reviewer`,
      :class:`~qorotrain.tuners.review.Decision`,
      :class:`~qorotrain.tuners.review.AutoApprove`).
    - :mod:`~qorotrain.tuners.types`: :class:`~qorotrain.tuners.types.Trial` and
      :class:`~qorotrain.tuners.types.Proposal`.

    Tuners are constructed from ``experiment.yaml`` by
    :func:`qorotrain.tuners.factory.create_tuner`.

===============================================================================
"""
from .review import AutoApprove, Decision, Reviewer
from .space import SearchSpace
from .tuner import AgenticTuner, GridTuner, OptunaTuner, Proposal, RandomTuner, StopTuning, Trial, Tuner

__all__ = [
    "AgenticTuner", "AutoApprove", "Decision", "GridTuner", "OptunaTuner", "Proposal", "RandomTuner",
    "Reviewer", "SearchSpace", "StopTuning", "Trial", "Tuner",
]
