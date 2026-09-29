"""Answers a process keeps for a few seconds, for a read that costs the forge
the same whoever asks: the public list of contests, which a visitor with no
session can ask for as often as they like. The first caller works the answer
out and the ones arriving meanwhile wait for it, so a burst of visitors costs
the forge one read, and the answer is kept for its time by the setup's clock.
Only an answer that cannot change under its holder is kept, since every
caller is handed the same one.
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

    async def remembered[T](
        self, name: str, keep: timedelta, work: Callable[[], Awaitable[T]]
    ) -> T:
        """The answer kept under `name`, or `work`'s, kept for `keep` from now.
        A caller arriving while `work` runs waits for its answer.
        """
        if self._fresh(name):
            return cast(T, self._kept[name][1])
        async with self._working.setdefault(name, asyncio.Lock()):
            if self._fresh(name):
                return cast(T, self._kept[name][1])
            answer = await work()
            self._kept[name] = (self.clock.now() + keep, answer)
            return answer

    def _fresh(self, name: str) -> bool:
        kept = self._kept.get(name)
        return kept is not None and self.clock.now() < kept[0]
