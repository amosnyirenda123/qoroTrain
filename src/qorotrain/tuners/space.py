"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/space.py

Description:
    The hyperparameters a tuner may change, and what it can do with them.

    :class:`SearchSpace` wraps the ``job.hyperparameters`` section of
    ``experiment.yaml`` (a mapping of name to
    :class:`~qorotrain.config.models.HyperParamSpec`) and provides the three
    operations the tuning algorithms need:

    - **validation** of a proposed assignment (used by the Tuner on every
      proposal and every reviewer edit);
    - **random sampling** (used by :class:`~qorotrain.tuners.tuner.RandomTuner`);
    - **grid construction** (used by :class:`~qorotrain.tuners.tuner.GridTuner`).

    Numerical conventions:
        Ranges marked ``log`` are sampled and interpolated uniformly in
        ``ln`` space, i.e. ``exp(U(ln min, ln max))``, so every order of
        magnitude gets equal weight. Float results are rounded to six
        significant digits for readability and then clipped to ``[min, max]``
        so rounding can never leave the range.

===============================================================================
"""
import itertools
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config.models import HyperParamSpec

if TYPE_CHECKING:
    from ..config.models import JobSpec

MAX_GRID_SIZE = 100_000
"""Upper bound on the number of grid cells, to fail fast on accidental explosions."""


def _tidy(x: float, low: float, high: float) -> float:
    """Six significant digits keeps proposals readable; clip so rounding never leaves the range.

    Args:
        x: Raw value.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        ``x`` rounded to 6 significant digits and clipped to ``[low, high]``.
    """
    return min(max(float(f"{x:.6g}"), low), high)


@dataclass(frozen=True)
class SearchSpace:
    """The set of tunable parameters and their allowed values.

    Attributes:
        params (dict[str, HyperParamSpec]): Parameter name to its range or
            choices, in declaration order. The order determines the order of
            keys in samples and the nesting order of the grid.
    """

    params: dict[str, HyperParamSpec]

    @classmethod
    def from_spec(cls, job: "JobSpec") -> "SearchSpace":
        """Build the search space of a job.

        Args:
            job: The ``job`` section of a loaded experiment.

        Returns:
            A search space over ``job.hyperparameters``.
        """
        return cls(dict(job.hyperparameters))

    @property
    def names(self) -> list[str]:
        """Names of the tunable parameters, in declaration order."""
        return list(self.params)

    # ------------------------------------------------------------- validation

    def validate(self, values: dict[str, Any]) -> list[str]:
        """Problems with a full assignment of the tunable parameters (empty when it is valid).

        Every problem is reported, not just the first: unknown names, missing
        names, and values rejected by
        :meth:`~qorotrain.config.models.HyperParamSpec.accepts`.

        Args:
            values: A complete mapping of parameter name to value.

        Returns:
            Human-readable problem descriptions; an empty list means valid.
        """
        problems = [f"unknown parameter '{n}'" for n in values if n not in self.params]
        for name, spec in self.params.items():
            if name not in values:
                problems.append(f"missing parameter '{name}'")
                continue
            reason = spec.accepts(values[name])
            if reason:
                problems.append(f"'{name}': {reason}")
        return problems

    # --------------------------------------------------------------- sampling

    def sample(self, rng: random.Random) -> dict[str, Any]:
        """One random configuration: uniform, or log-uniform for ranges marked ``log``.

        Args:
            rng: Random generator; passing a seeded generator makes the
                sequence of samples reproducible.

        Returns:
            A complete assignment of every parameter.
        """
        return {name: self._sample_one(spec, rng) for name, spec in self.params.items()}

    @staticmethod
    def _sample_one(spec: HyperParamSpec, rng: random.Random) -> Any:
        """Draw one value for a single parameter.

        ``choices`` are drawn uniformly. Integer ranges use ``randint`` (both
        ends inclusive) or, when ``log``, a rounded log-uniform draw clipped to
        the range. Float ranges are uniform or log-uniform, then tidied.

        Args:
            spec: The parameter's specification.
            rng: Random generator.

        Returns:
            An ``int`` or ``float`` (or one of the choices) within the spec.
        """
        if spec.choices is not None:
            return rng.choice(spec.choices)
        low, high = spec.min, spec.max
        if spec.type == "int":
            if spec.log:
                value = round(math.exp(rng.uniform(math.log(low), math.log(high))))
                return int(min(max(value, low), high))
            return rng.randint(int(low), int(high))
        value = math.exp(rng.uniform(math.log(low), math.log(high))) if spec.log else rng.uniform(low, high)
        return _tidy(value, low, high)

    # ------------------------------------------------------------------- grid

    def axis(self, name: str, points: int) -> list[Any]:
        """Candidate values of one parameter: its choices, or ``points`` values interpolated
        between min and max (evenly spaced, or evenly spaced in log space for ``log`` ranges).

        For fractions ``f_i = i / (points - 1)``, the linear axis is
        ``min + f_i * (max - min)`` and the log axis is
        ``exp(ln min + f_i * (ln max - ln min))``. Integer axes are rounded and
        de-duplicated, so a narrow integer range may yield fewer than
        ``points`` values.

        Args:
            name: Parameter name; must be in ``params``.
            points: Number of values for a range (at least 2; not checked
                here, see :meth:`grid`).

        Returns:
            The candidate values, in increasing order for ranges.

        Raises:
            KeyError: If ``name`` is not a parameter of this space.
        """
        spec = self.params[name]
        if spec.choices is not None:
            return list(spec.choices)

        low, high = spec.min, spec.max
        fractions = [i / (points - 1) for i in range(points)]
        if spec.log:
            raw = [math.exp(math.log(low) + f * (math.log(high) - math.log(low))) for f in fractions]
        else:
            raw = [low + f * (high - low) for f in fractions]

        if spec.type == "int":
            values = [int(min(max(round(v), low), high)) for v in raw]
            return list(dict.fromkeys(values))  # a narrow int range can collapse neighbours
        return [_tidy(v, low, high) for v in raw]

    def grid_size(self, points: int) -> int:
        """Number of cells in the grid, without building it.

        Args:
            points: Values per range, as for :meth:`axis`.

        Returns:
            The product of the axis lengths.
        """
        size = 1
        for name in self.params:
            size *= len(self.axis(name, points))
        return size

    def grid(self, points: int) -> list[dict[str, Any]]:
        """Every combination of the per-parameter axes.

        Args:
            points: Values per range (at least 2).

        Returns:
            The Cartesian product of the axes, as complete assignments, in
            ``itertools.product`` order (last parameter varies fastest).

        Raises:
            ValueError: If ``points < 2`` or the grid would exceed
                :data:`MAX_GRID_SIZE` cells.
        """
        if points < 2:
            raise ValueError("a grid needs at least 2 points per parameter")
        size = self.grid_size(points)
        if size > MAX_GRID_SIZE:
            raise ValueError(f"the grid has {size} cells; lower the number of points or tune fewer parameters")
        axes = [self.axis(name, points) for name in self.params]
        return [dict(zip(self.params, combo)) for combo in itertools.product(*axes)]
