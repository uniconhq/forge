"""Actions and building blocks. An action is a service function the backend
may call, marked `@action`. Called with no `Context`, it opens a unit of
work on a setup, runs, commits when it returns, rolls back when it raises,
and closes the transaction either way. Called with a `Context`, as another
service calls it, it runs straight through inside the caller's unit of
work. A service function without the mark is a building block, and only
another part of the package, which holds a `Context`, can call it.

An action opens its unit of work on the setup the process holds, unless it
is handed another setup as its first argument, which is how a test runs one
against a setup of its own.
"""

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate, Protocol, cast, overload

from forge.runtime.context import ActionSetup, Context
from forge.runtime.held import held


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
