"""The broker one process keeps for live updates: the streams its sessions
hold open, and one connection that listens for the nudges every process
publishes through Postgres (`NOTIFY` on `live.CHANNEL`), so a nudge reaches
a stream whichever process wrote it or took the forge's push.

The connection is opened when the first stream subscribes and closed when
the last one leaves, so a process nobody is watching holds nothing and a
test or an operator's command never opens it. It is outside the pool,
which is why a process uses one connection more than its pool while anyone
is watching. When it drops it is opened again a second later, and every
stream is told to `resync`, since nudges sent meanwhile were missed.

Each stream has a queue of `QUEUE_MOST` nudges. A stream that falls that
far behind, because its browser stopped reading, has its queue emptied and
is told to `resync` instead, so one slow reader never holds up the rest or
grows without bound. A nudge goes only to a stream whose `Audience` hears
it.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import psycopg

from forge.domain.live import CHANNEL, Audience, Nudge, NudgeKind, hears, read_payload
from forge.log import get_logger

log = get_logger(__name__)

QUEUE_MOST = 256
RETRY_SECONDS = 1.0
RESYNC = Nudge(NudgeKind.RESYNC, "")


@dataclass(eq=False)
class Subscription:
    """One stream's place in the broker: who it speaks to, which its owner
    may change, and the nudges waiting for it.
    """

    audience: Audience
    queue: asyncio.Queue[Nudge] = field(default_factory=lambda: asyncio.Queue(QUEUE_MOST))

    def offer(self, nudge: Nudge) -> None:
        if not hears(self.audience, nudge):
            return
        try:
            self.queue.put_nowait(nudge)
        except asyncio.QueueFull:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(RESYNC)


class Broker:
    def __init__(self, database_url: str) -> None:
        self._url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._subscriptions: set[Subscription] = set()
        self._listening: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()

    @contextlib.asynccontextmanager
    async def subscription(self, audience: Audience) -> AsyncIterator[Subscription]:
        """A stream's subscription for as long as the block runs, listening
        from the first one on.
        """
        subscription = Subscription(audience)
        self._subscriptions.add(subscription)
        if self._listening is None or self._listening.done():
            self.ready.clear()
            self._listening = asyncio.create_task(self._listen())
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)
            if not self._subscriptions:
                await self.stop()

    def deliver(self, nudge: Nudge) -> None:
        """Hand a nudge to every stream that hears it."""
        for subscription in list(self._subscriptions):
            subscription.offer(nudge)

    async def stop(self) -> None:
        listening, self._listening = self._listening, None
        if listening is not None and not listening.done():
            listening.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listening

    async def _listen(self) -> None:
        missed = False
        while self._subscriptions:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._url, autocommit=True
                ) as connection:
                    await connection.execute(f"LISTEN {CHANNEL}")
                    self.ready.set()
                    if missed:
                        self.deliver(RESYNC)
                    async for notify in connection.notifies():
                        nudge = read_payload(notify.payload)
                        if nudge is None:
                            log.warning("broker.payload_unread", channel=notify.channel)
                            continue
                        self.deliver(nudge)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("broker.listen_lost", error=type(exc).__name__, detail=str(exc))
                self.ready.clear()
                missed = True
                await asyncio.sleep(RETRY_SECONDS)
