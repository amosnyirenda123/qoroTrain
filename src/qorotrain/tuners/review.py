"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/tuners/review.py

Description:
    Human-in-the-loop hook between a tuning algorithm and the next run.

    Every proposal passes through a :class:`Reviewer` before it is launched.
    The reviewer can approve it, approve an edited version, or reject it (which
    ends the session). The Tuner validates edits against the search space
    again, so a reviewer cannot launch out-of-range values.

    Only :class:`AutoApprove` exists today. Manual review
    (``tuner.review.mode: manual``) is declared in the schema, but
    :func:`~qorotrain.tuners.factory.create_tuner` refuses it unless a custom
    reviewer is passed in programmatically.

    Threading:
        ``Reviewer.review`` is called on the Tuner's decision thread, never on
        the Bridge's dispatch thread, so it may block (for example on user
        input) without stalling message delivery.

===============================================================================
"""
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .types import Proposal, Trial


@dataclass(frozen=True)
class Decision:
    """The outcome of a review.

    ``approve`` launches the proposal, or ``params`` when the reviewer edited it (edits are
    validated against the search space again). ``reject`` ends the tuning session.

    Attributes:
        action (Literal["approve", "reject"]): What to do with the proposal.
        params (dict[str, Any] | None): Edited values, merged over the
            proposal's parameters. ``None`` means "run the proposal as is".
        reason (str): Explanation; included in the stop reason on rejection.
    """

    action: Literal["approve", "reject"] = "approve"
    params: dict[str, Any] | None = None
    reason: str = ""


class Reviewer(Protocol):
    """Called on the tuner's decision thread, so it may block (for example on user input)."""

    def review(self, proposal: Proposal, history: list[Trial]) -> Decision:
        """Decide what happens to a proposal.

        Args:
            proposal: The complete, already validated proposal.
            history: Every trial so far, oldest first.

        Returns:
            The reviewer's decision.
        """
        ...


class AutoApprove:
    """Approves everything. Used for unattended runs and for benchmarks."""

    def review(self, proposal: Proposal, history: list[Trial]) -> Decision:
        """Approve ``proposal`` unchanged.

        Args:
            proposal: The proposal under review (ignored).
            history: The trial history (ignored).

        Returns:
            ``Decision("approve")``.
        """
        return Decision("approve")
