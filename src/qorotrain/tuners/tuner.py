from abc import ABC, abstractmethod
from typing import Literal

from ..routes.route import Route
from ..messages.message import Message


class Tuner(ABC):

    def __init__(
            self,
            mode: Literal['agentic', 'randomized'],
            store_path: str | None = None):
        self._mode = mode
        self._store_path = store_path

    def start(self):
        pass

    @abstractmethod
    def update(self, route: Route, message: Message): ...

    def stop(self):
        pass