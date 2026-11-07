"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/routes/route.py

Description:
    Topic routing primitives: MQTT-style topic filters and the :class:`Route`
    that binds a filter to a handler.

    Connectors deliver every incoming message to the Bridge; the Bridge then
    asks each registered :class:`Route` whether it :meth:`~Route.matches` the
    message's topic and calls the handlers of all matching routes. Matching is
    implemented here, in plain Python, rather than relying on the broker, so
    that routing behaves the same for every connector (including the in-memory
    fakes used in tests).

    Topic filter syntax:
        As in MQTT 3.1.1 and 5. Topics are ``/``-separated levels. ``+`` matches exactly one level;
        ``#`` matches any number of remaining levels (including zero) and must
        be the last level. Wildcards must occupy a whole level.

===============================================================================
"""
import uuid
from dataclasses import dataclass, field
from typing import Callable

from ..messages.message import Message

Handler = Callable[[Message], None]
"""Signature of a route handler, which receives one message and returns nothing."""


def validate_pattern(pattern: str) -> None:
    """Raise ValueError if ``pattern`` is not a valid MQTT-style topic filter.

    Args:
        pattern: Topic filter to check, for example ``tuning/demo/+/status``.

    Raises:
        ValueError: If the pattern is empty, ``#`` is not a whole final level,
            or ``+`` is not a whole level.
    """
    if not pattern:
        raise ValueError("Topic pattern must not be empty.")
    parts = pattern.split("/")
    for i, seg in enumerate(parts):
        if "#" in seg and (seg != "#" or i != len(parts) - 1):
            raise ValueError(f"'#' must be a whole last level in {pattern!r}.")
        if "+" in seg and seg != "+":
            raise ValueError(f"'+' must be a whole level in {pattern!r}.")


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard matching: '+' is one level, a trailing '#' is any remainder.

    The comparison walks the two level lists in lockstep: literal levels must
    be equal, ``+`` accepts any single level, and ``#`` accepts everything that
    remains (so ``a/#`` matches ``a``, ``a/b`` and ``a/b/c``). Otherwise both
    lists must have the same length.

    Args:
        pattern: A valid topic filter (see :func:`validate_pattern`).
        topic: A concrete topic, without wildcards.

    Returns:
        ``True`` if ``topic`` matches ``pattern``.
    """
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

    When several routes share a topic filter, the Bridge subscribes once with
    the highest explicit QoS among them.

    Attributes:
        topic (str): Topic filter, validated on construction.
        handler (Handler): Callable invoked with each matching message, on the
            Bridge's dispatch thread.
        qos (int | None): Requested subscription QoS, or ``None``.
        route_id (str): Random 8-hex-digit identifier, used as the primary
            key in the Bridge's routing table. Not a constructor argument.

    Raises:
        ValueError: On construction, if ``topic`` is not a valid filter or
            ``qos`` is not ``None``, 0, 1 or 2.
    """

    topic: str
    handler: Handler
    qos: int | None = None
    route_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8], init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the topic filter and QoS.

        Raises:
            ValueError: If the topic filter or QoS is invalid.
        """
        validate_pattern(self.topic)
        if self.qos not in (None, 0, 1, 2):
            raise ValueError("qos must be None, 0, 1 or 2.")

    def matches(self, topic: str) -> bool:
        """Tell whether a concrete topic is covered by this route's filter.

        Args:
            topic: Topic of an incoming message.

        Returns:
            ``True`` if the handler should receive the message.
        """
        return topic_matches(self.topic, topic)

    @property
    def handler_name(self) -> str:
        """Dotted ``module.qualname`` of the handler, for logs and the routing table."""
        module = getattr(self.handler, "__module__", "?")
        name = getattr(self.handler, "__qualname__", repr(self.handler))
        return f"{module}.{name}"
