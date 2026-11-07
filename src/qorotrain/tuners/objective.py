"""Turn a metric stream into the single score of a run."""
import math
import re

_MEAN_LAST = re.compile(r"^mean_last_([1-9][0-9]*)$")


def aggregate(values: list[float], how: str, direction: str) -> float | None:
    """Score a run from the values of its objective metric.

    ``how`` is one of ``last``, ``best``, ``mean`` or ``mean_last_N`` (the mean of the last N
    values, or of all of them when there are fewer). Returns None when there is nothing usable:
    no values, or a NaN/infinite result, which means the run diverged.
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
    if incumbent is None:
        return True
    return candidate > incumbent if direction == "maximize" else candidate < incumbent