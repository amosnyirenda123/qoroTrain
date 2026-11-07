from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .types import Proposal, Trial


@dataclass(frozen=True)
class Decision:
    """The outcome of a review.

    ``approve`` launches the proposal, or ``params`` when the reviewer edited it (edits are
    validated against the search space again). ``reject`` ends the tuning session.
    """

    action: Literal["approve", "reject"] = "approve"
    params: dict[str, Any] | None = None
    reason: str = ""


class Reviewer(Protocol):
    """Called on the tuner's decision thread, so it may block (for example on user input)."""

    def review(self, proposal: Proposal, history: list[Trial]) -> Decision: ...


class AutoApprove:
    """Approves everything. Used for unattended runs and for benchmarks."""

    def review(self, proposal: Proposal, history: list[Trial]) -> Decision:
        return Decision("approve")