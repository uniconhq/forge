"""The one setup the process holds. `forge.api.start` builds it and holds it
here; an action called with no setup, and the plain functions beside the
actions, use it through `setup_or_held`. This module is the only thing that
touches the global.
"""

from typing import Protocol

from forge.runtime.context import ActionSetup


class HeldSetup(ActionSetup, Protocol):
    """What the process does with the setup it holds, beyond what an action
    takes from it: ask it whether it is ready, and stop it.
    """

    async def ready(self) -> None: ...

    async def stop(self) -> None: ...


_held: HeldSetup | None = None


def held() -> HeldSetup:
    """The setup the process holds. Raises when `forge.api.start` has not been
    called, rather than building one from whatever is in the environment.
    """
    if _held is None:
        raise RuntimeError("forge.api.start has not been called")
    return _held


def setup_or_held(setup: ActionSetup | None) -> ActionSetup:
    """`setup` when a caller hands one in, as a test does, and otherwise the
    setup the process holds.
    """
    return setup if setup is not None else held()


def holding() -> bool:
    return _held is not None


def hold(setup: HeldSetup) -> None:
    """Make `setup` the one the process holds. Refused while another is held."""
    global _held
    if _held is not None:
        raise RuntimeError("forge already holds a setup; call forge.api.stop first")
    _held = setup


def release() -> HeldSetup:
    """Stop holding the setup and return it, for the caller to stop."""
    global _held
    setup = held()
    _held = None
    return setup
