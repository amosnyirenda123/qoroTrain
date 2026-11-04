from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..routes.route import Route

if TYPE_CHECKING:
    from ..bridges.bridge import Bridge


@dataclass
class Trial:
    """One finished (or failed) training run, as seen by a tuning algorithm."""

    params: dict[str, Any]
    objective: float | None  # None if the run failed
    summary: dict[str, Any] = field(default_factory=dict)  # compact telemetry summary
    status: str = "finished"


@dataclass
class Proposal:
    params: dict[str, Any]
    rationale: str = ""


class Tuner(ABC):
    """Owns the plumbing; subclasses only decide what to try next.

    Message handling, collection, validation, review and publishing of start
    commands belong here. A subclass implements :meth:`propose` and nothing else.
    """

    def __init__(self, store_path: str | None = None) -> None:
        self._store_path = store_path  # metric store
        self._bridge: "Bridge | None" = None

    def start(self, bridge: "Bridge") -> None:
        """Called by the bridge once its connectors are up; keep it to publish commands."""
        self._bridge = bridge

    def stop(self) -> None:
        self._bridge = None

    def routes(self) -> list[Route]:
        """Routes the bridge should register for this tuner (decorators will fill this in)."""
        return []

    @abstractmethod
    def propose(self, history: list[Trial]) -> Proposal: ...


class AgenticTuner(Tuner):
    def propose(self, history: list[Trial]) -> Proposal:
        raise NotImplementedError


class RandomTuner(Tuner):
    def propose(self, history: list[Trial]) -> Proposal:
        raise NotImplementedError


class OptunaTuner(Tuner):
    def propose(self, history: list[Trial]) -> Proposal:
        raise NotImplementedError