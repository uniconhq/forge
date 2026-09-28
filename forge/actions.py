"""Actions and building blocks. An action is a service function the backend
may call, marked `@action`. Called with no `Context`, it opens a unit of
work on a setup, runs, commits when it returns, rolls back when it raises,
and closes the transaction either way. Called with a `Context`, as another
service calls it, it runs straight through inside the caller's unit of
work. A service function without the mark is a building block, and only
another part of the package, which holds a `Context`, can call it.

The process holds one setup, which `forge.api.start` builds. An action opens
its unit of work on that one, unless it is handed another setup as its
first argument, which is how a test runs one against a setup of its own.
"""

import functools
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any, Concatenate, Protocol, cast, overload, runtime_checkable

from forge.context import Clock, Context
from forge.port import Forge
from forge.settings import Settings


@runtime_checkable
class ActionSetup(Protocol):
    """What an action and the plain functions beside it take from a setup."""

    @property
    def forge(self) -> Forge: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def settings(self) -> Settings: ...

    def unit_of_work(self) -> AbstractAsyncContextManager[Context]: ...

    async def ready(self) -> None: ...

    async def stop(self) -> None: ...


class Action[**P, R](Protocol):
    """A service function marked `@action`, as its callers see it: first a
    `Context` to run inside, or a setup to open the unit of work on, or
    neither to open it on the setup the process holds.
    """

    @overload
    def __call__(
        self, ctx: Context, /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    @overload
    def __call__(
        self, setup: ActionSetup, /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...

    @overload
    def __call__(self, /, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R]: ...


_held: ActionSetup | None = None


def action[**P, R](work: Callable[Concatenate[Context, P], Awaitable[R]]) -> Action[P, R]:
    """Mark `work` as an action. It is written once, with a `Context` first,
    and callable in the three ways `Action` describes.
    """

    @functools.wraps(work)
    async def run(*args: Any, **kwargs: Any) -> Any:
        first = args[0] if args else None
        if isinstance(first, Context):
            return await work(*args, **kwargs)
        if isinstance(first, ActionSetup):
            setup, rest = first, args[1:]
        else:
            setup, rest = held(), args
        async with setup.unit_of_work() as ctx:
            return await work(ctx, *rest, **kwargs)

    return cast(Action[P, R], run)


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
