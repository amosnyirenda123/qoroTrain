from dataclasses import dataclass, field
from typing import Mapping

@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    headers: Mapping[str, str] = field(default_factory=dict)