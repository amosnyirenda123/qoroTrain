"""The hyperparameters a tuner may change, and what it can do with them."""
import itertools
import math
import random
from dataclasses import dataclass
from typing import Any

from ..config.models import HyperParamSpec

MAX_GRID_SIZE = 100_000


def _tidy(x: float, low: float, high: float) -> float:
    """Six significant digits keeps proposals readable; clip so rounding never leaves the range."""
    return min(max(float(f"{x:.6g}"), low), high)


@dataclass(frozen=True)
class SearchSpace:
    params: dict[str, HyperParamSpec]

    @classmethod
    def from_spec(cls, job) -> "SearchSpace":
        return cls(dict(job.hyperparameters))

    @property
    def names(self) -> list[str]:
        return list(self.params)

    # ------------------------------------------------------------- validation

    def validate(self, values: dict[str, Any]) -> list[str]:
        """Problems with a full assignment of the tunable parameters (empty when it is valid)."""
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
        """One random configuration: uniform, or log-uniform for ranges marked ``log``."""
        return {name: self._sample_one(spec, rng) for name, spec in self.params.items()}

    @staticmethod
    def _sample_one(spec: HyperParamSpec, rng: random.Random) -> Any:
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
        between min and max (evenly spaced, or evenly spaced in log space for ``log`` ranges)."""
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
        size = 1
        for name in self.params:
            size *= len(self.axis(name, points))
        return size

    def grid(self, points: int) -> list[dict[str, Any]]:
        """Every combination of the per-parameter axes."""
        if points < 2:
            raise ValueError("a grid needs at least 2 points per parameter")
        size = self.grid_size(points)
        if size > MAX_GRID_SIZE:
            raise ValueError(f"the grid has {size} cells; lower the number of points or tune fewer parameters")
        axes = [self.axis(name, points) for name in self.params]
        return [dict(zip(self.params, combo)) for combo in itertools.product(*axes)]