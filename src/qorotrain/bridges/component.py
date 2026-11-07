"""
===============================================================================
Author: Amos Nyirenda
Date: 2026-11-07
Last Updated: 2026-11-07
File: qorotrain/bridges/component.py

Description:
    Structural interface for anything the Bridge can host.

    The Bridge does not know about Tuners or Runners specifically; it hosts
    *components*. A component declares the topics it listens to
    (:meth:`Component.routes`), receives the running Bridge so it can publish
    (:meth:`Component.start`), and releases its resources on
    :meth:`Component.stop`. :class:`~qorotrain.tuners.tuner.Tuner`,
    :class:`~qorotrain.runners.runner.Runner` and test probes all satisfy it
    without inheriting from anything (``typing.Protocol``, duck typing).

    Lifecycle contract:
        Enforced by :class:`~qorotrain.bridges.bridge.Bridge`.

        1. ``routes()`` is called once, when the component is attached.
        2. ``start(bridge)`` is called after the Bridge is connected and
           subscribed (immediately, if the Bridge is already running).
        3. ``stop()`` is called on detach or when the Bridge closes, in reverse
           attach order, only if ``start`` was called.

===============================================================================
"""
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..routes.route import Route

if TYPE_CHECKING:
    from .bridge import Bridge


@runtime_checkable
class Component(Protocol):
    """Anything the bridge can host: a Tuner, a Runner, a test probe.

    ``routes()`` says what to listen to, ``start(bridge)`` hands over the bridge
    (to publish with), and ``stop()`` releases resources.

    The protocol is ``runtime_checkable``, so ``isinstance(obj, Component)``
    checks that the three methods exist (not their signatures).
    """

    def routes(self) -> list[Route]:
        """Return the routes (topic filter + handler) this component needs.

        Handlers run on the Bridge's single dispatch thread and must return
        quickly.

        Returns:
            The routes to register with the Bridge.
        """
        ...

    def start(self, bridge: "Bridge") -> None:
        """Begin operating; keep ``bridge`` to publish messages.

        Args:
            bridge: The running Bridge hosting this component.
        """
        ...

    def stop(self) -> None:
        """Stop operating and release resources (threads, processes)."""
        ...
