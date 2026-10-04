"""Answers a process keeps for a few seconds, for a read that costs the forge
the same whoever asks: every published contest, which every visitor's and
every signed-in person's list of contests starts from. The first caller
works the answer out and the ones arriving meanwhile wait for it, so a burst
of visitors costs the forge one read, and the answer is kept for its time by
the setup's clock. Only an answer that cannot change under its holder is
kept, since every caller is handed the same one. `forget` drops an answer
this process knows it has just made stale, so the next caller works it out
again; another process's copy lasts its time.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from forge.domain.clock import Clock


@dataclass
class Memo:
    """Kept answers by name, each until the clock reaches its time."""

    clock: Clock
    _kept: dict[str, tuple[datetime, Any]] = field(default_factory=dict)
    _working: dict[str, asyncio.Lock] = field(default_factory=dict)
    _forgotten: dict[str, int] = field(default_factory=dict)

    async def remembered[T](
        self, name: str, keep: timedelta, work: Callable[[], Awaitable[T]]
    ) -> T:
        """The answer kept under `name`, or `work`'s, kept for `keep` from now.
        A caller arriving while `work` runs waits for its answer. An answer
        forgotten while `work` ran is handed to its callers and not kept,
        since it may have been read before the change that made it stale.
        """
        if self._fresh(name):
            return cast(T, self._kept[name][1])
        async with self._working.setdefault(name, asyncio.Lock()):
            if self._fresh(name):
                return cast(T, self._kept[name][1])
            began = self._forgotten.get(name, 0)
            answer = await work()
            if self._forgotten.get(name, 0) == began:
                self._kept[name] = (self.clock.now() + keep, answer)
            return answer

    def forget(self, name: str) -> None:
        """Drop the answer kept under `name`, and any being worked out now."""
        self._kept.pop(name, None)
        self._forgotten[name] = self._forgotten.get(name, 0) + 1

    def _fresh(self, name: str) -> bool:
        kept = self._kept.get(name)
        return kept is not None and self.clock.now() < kept[0]
