"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/types.py

Description:
    Value types exchanged between the tuning loop, the tuning algorithms and
    the reviewer.

    :class:`Trial` is what an algorithm learns from (the history of finished
    runs); :class:`Proposal` is what it answers with. Both are plain
    dataclasses with no behaviour so that algorithms stay independent of the
    messaging and storage machinery in :class:`~qorotrain.tuners.tuner.Tuner`.

===============================================================================
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trial:
    """One finished (or failed) training run, as seen by a tuning algorithm.

    Attributes:
        params (dict[str, Any]): The complete set of tunable parameter values
            the run was launched with.
        objective (float | None): The run's score, computed by
            :func:`~qorotrain.tuners.objective.aggregate`. ``None`` if the run
            failed, was stopped, logged no values, or diverged (NaN/inf).
        summary (dict[str, Any]): Compact telemetry summary. Currently holds
            ``points`` (number of objective values), ``last`` (the final
            value, when there is one) and ``error`` (why there is no
            objective, when applicable).
        status (str): ``finished``, ``failed`` or ``stopped``. A run that
            exited cleanly but produced no usable objective is ``failed``.
        run (str): Run identifier, ``<session>-<counter>``.
    """

    params: dict[str, Any]
    objective: float | None  # None if the run failed or produced no usable objective
    summary: dict[str, Any] = field(default_factory=dict)  # compact telemetry summary
    status: str = "finished"  # finished | failed | stopped
    run: str = ""


@dataclass
class Proposal:
    """The configuration an algorithm wants to try next.

    Attributes:
        params (dict[str, Any]): Parameter values to run. May be partial: the
            Tuner fills in the omitted names from the previous run before
            validating it against the search space.
        rationale (str): Human-readable explanation, shown to the reviewer.
    """

    params: dict[str, Any]
    rationale: str = ""
