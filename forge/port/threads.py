"""Announcements and clarifications: threads held at a contest, a task or a
workspace's desk, labelled by kind.
"""

from typing import Protocol

from forge.domain.identity import Identity
from forge.domain.ids import ThreadId
from forge.domain.threads import Thread, ThreadKind, ThreadPlace


class ThreadPort(Protocol):
    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        """Start a thread at the place. `Forbidden` when the identity may not
        post there.
        """
        ...

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]:
        """Every thread of the kind at the place, with its comments.
        `Forbidden` when the identity may not read the place.
        """
        ...

    async def edit_thread(
        self, as_: Identity, thread: ThreadId, *, title: str, body: str
    ) -> None: ...

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None: ...

    async def comment(
        self, as_: Identity, thread: ThreadId, body: str, *, answered: bool = False
    ) -> None:
        """Add a comment; with `answered` the thread is marked answered and
        closed in the same call.
        """
        ...
