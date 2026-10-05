"""Turns: at most a few holders of a piece of work at a time in a process,
the rest waiting in order, shared by the services that make places and send
mail.
"""

import asyncio
import weakref


class Turns:
    """At most `at_once` holders at a time in a process, the rest waiting
    their turn in order, with one set of turns per event loop, since a
    semaphore belongs to the loop it is first used on.
    """

    def __init__(self, at_once: int) -> None:
        self.at_once = at_once
        self._by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    def __call__(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        turns = self._by_loop.get(loop)
        if turns is None:
            turns = self._by_loop[loop] = asyncio.Semaphore(self.at_once)
        return turns
