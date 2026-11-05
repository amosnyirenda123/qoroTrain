from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..routes.route import Route

if TYPE_CHECKING:
    from .bridge import Bridge


@runtime_checkable
class Component(Protocol):
    """Anything the bridge can host: a Tuner, a Runner, a test probe.

    ``routes()`` says what to listen to, ``start(bridge)`` hands over the bridge
    (to publish with), and ``stop()`` releases resources.
    """

    def routes(self) -> list[Route]: ...

    def start(self, bridge: "Bridge") -> None: ...

    def stop(self) -> None: ...