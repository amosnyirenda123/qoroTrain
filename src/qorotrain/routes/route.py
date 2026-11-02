from dataclasses import dataclass
from typing import Callable
from ..messages.message import Message

@dataclass
class Route:
    topic: str
    handler: Callable[[Message], None] 