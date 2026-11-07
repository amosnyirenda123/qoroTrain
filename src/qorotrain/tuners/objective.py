"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/objective.py

Description:
    Turn a metric stream into the single score of a run.

    A notebook streams many values of its objective metric (one per step);
    tuning algorithms compare runs by one number. :func:`aggregate` performs
    that reduction according to ``job.objective.aggregate``, and
    :func:`is_better` compares two scores according to
    ``job.objective.direction``.

    Divergence handling:
        NaN and infinite values are preserved end to end (the Tracker sends
        them, the store keeps them) precisely so that this module can treat a
        diverged run as having *no* objective rather than a misleading number.

===============================================================================
"""
import math
import re

_MEAN_LAST = re.compile(r"^mean_last_([1-9][0-9]*)$")


def aggregate(values: list[float], how: str, direction: str) -> float | None:
    """Score a run from the values of its objective metric.

    ``how`` is one of ``last``, ``best``, ``mean`` or ``mean_last_N`` (the mean of the last N
    values, or of all of them when there are fewer). Returns None when there is nothing usable:
    no values, or a NaN/infinite result, which means the run diverged.

    ``best`` ignores non-finite values and picks the maximum or minimum of the
    rest; ``last``, ``mean`` and ``mean_last_N`` propagate NaN/inf, so a single
    diverged value inside the window makes the whole score ``None``.

    Args:
        values: The metric's values in step order.
        how: Aggregation rule, as validated by
            :class:`~qorotrain.config.models.ObjectiveSpec`.
        direction: ``"maximize"`` or ``"minimize"``; only used by ``best``.

    Returns:
        The finite score, or ``None`` if there is no usable score.

    Raises:
        ValueError: If ``how`` is not a recognised aggregation rule.
    """
    if not values:
        return None

    if how == "last":
        score = values[-1]
    elif how == "best":
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return None
        score = max(finite) if direction == "maximize" else min(finite)
    elif how == "mean":
        score = sum(values) / len(values)
    elif match := _MEAN_LAST.match(how):
        tail = values[-int(match.group(1)):]
        score = sum(tail) / len(tail)
    else:
        raise ValueError(f"unknown aggregate {how!r}")

    return score if math.isfinite(score) else None


def is_better(candidate: float, incumbent: float | None, direction: str) -> bool:
    """Tell whether ``candidate`` strictly improves on ``incumbent``.

    Ties are not improvements, so the earliest of equally good runs stays the
    best one (this also drives the ``patience`` stop rule).

    Args:
        candidate: The new score.
        incumbent: The best score so far, or ``None`` if there is none.
        direction: ``"maximize"`` or ``"minimize"``.

    Returns:
        ``True`` if there is no incumbent or ``candidate`` is strictly better.
    """
    if incumbent is None:
        return True
    return candidate > incumbent if direction == "maximize" else candidate < incumbent
