import uuid
from dataclasses import dataclass, field
from typing import Callable

from ..messages.message import Message

Handler = Callable[[Message], None]


def validate_pattern(pattern: str) -> None:
    """Raise ValueError if ``pattern`` is not a valid MQTT-style topic filter."""
    if not pattern:
        raise ValueError("Topic pattern must not be empty.")
    parts = pattern.split("/")
    for i, seg in enumerate(parts):
        if "#" in seg and (seg != "#" or i != len(parts) - 1):
            raise ValueError(f"'#' must be a whole last level in {pattern!r}.")
        if "+" in seg and seg != "+":
            raise ValueError(f"'+' must be a whole level in {pattern!r}.")


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard matching: '+' is one level, a trailing '#' is any remainder."""
    p = pattern.split("/")
    t = topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return i == len(p) - 1
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(p) == len(t)


@dataclass(eq=False)
class Route:
    """Delivers messages whose topic matches ``topic`` to ``handler``.

    Routes compare by identity, so two routes with the same topic and handler
    are distinct. ``qos=None`` means "use the connector's default".
    """

    topic: str
    handler: Handler
    qos: int | None = None
    route_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8], init=False, repr=False)

    def __post_init__(self) -> None:
        validate_pattern(self.topic)
        if self.qos not in (None, 0, 1, 2):
            raise ValueError("qos must be None, 0, 1 or 2.")

    def matches(self, topic: str) -> bool:
        return topic_matches(self.topic, topic)

    @property
    def handler_name(self) -> str:
        module = getattr(self.handler, "__module__", "?")
        name = getattr(self.handler, "__qualname__", repr(self.handler))
        return f"{module}.{name}"