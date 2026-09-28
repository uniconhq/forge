"""The one setup the process holds. `forge.api.start` builds it and holds it
here; an action called with no setup, and the plain functions beside the
actions, use it. This module is the only thing that touches the global.
"""

from forge.runtime.context import ActionSetup

_held: ActionSetup | None = None


def held() -> ActionSetup:
    """The setup the process holds. Raises when `forge.api.start` has not been
    called, rather than building one from whatever is in the environment.
    """
    if _held is None:
        raise RuntimeError("forge.api.start has not been called")
    return _held


def holding() -> bool:
    return _held is not None


def hold(setup: ActionSetup) -> None:
    """Make `setup` the one the process holds. Refused while another is held."""
    global _held
    if _held is not None:
        raise RuntimeError("forge already holds a setup; call forge.api.stop first")
    _held = setup


def release() -> ActionSetup:
    """Stop holding the setup and return it, for the caller to stop."""
    global _held
    setup = held()
    _held = None
    return setup
