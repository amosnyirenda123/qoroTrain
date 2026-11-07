from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trial:
    """One finished (or failed) training run, as seen by a tuning algorithm."""

    params: dict[str, Any]
    objective: float | None  # None if the run failed or produced no usable objective
    summary: dict[str, Any] = field(default_factory=dict)  # compact telemetry summary
    status: str = "finished"  # finished | failed | stopped
    run: str = ""


@dataclass
class Proposal:
    params: dict[str, Any]
    rationale: str = ""