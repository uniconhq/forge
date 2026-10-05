"""Announcements and clarifications: threads held at a contest, a task or a
workspace's desk, labelled by kind. Every change is one call made as the
identity handed in, so the forge's record says who made it. Marking a thread
answered and taking the mark off again are each safe to repeat, and there is
no delete: a thread people have read is closed, never removed. The event the
forge pushes when a thread changes is read here too, into the platform's
words, since its shape is the forge's own.
"""

from typing import Protocol

from forge.domain.identity import Identity
from forge.domain.ids import OrgId, ThreadId
from forge.domain.threads import Thread, ThreadChange, ThreadKind, ThreadPlace


class ThreadPort(Protocol):
    def thread_of(self, place: ThreadPlace, number: int) -> ThreadId:
        """The id of the thread numbered `number` at the place. No call is
        made.
        """
        ...

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> Thread:
        """Start a thread at the place. `Forbidden` when the identity may not
        post there.
        """
        ...

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, comments: bool = True
    ) -> tuple[Thread, ...]:
        """Every thread of the kind at the place, open and closed, oldest
        first, with its comments unless `comments` is off, which saves a call
        a thread. `Forbidden` when the identity may not read the place.
        """
        ...

    async def read_thread(self, as_: Identity, thread: ThreadId) -> Thread:
        """One thread with its comments. `NotFound` for none, or one the
        identity may not read.
        """
        ...

    async def search_threads(
        self,
        as_: Identity,
        org: OrgId,
        kind: ThreadKind,
        *,
        open_only: bool = True,
        comments: bool = True,
    ) -> tuple[Thread, ...]:
        """Every thread of the kind anywhere in the org that the identity may
        read, the open ones alone unless `open_only` is off, oldest first,
        with its comments unless `comments` is off: one search at the forge,
        not a list built place by place. Comments cost a call a thread, so a
        caller that keeps a few of many reads those few again instead.
        """
        ...

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        """Change the thread's title and text."""
        ...

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        """Close the thread, which stays readable."""
        ...

    async def comment(self, as_: Identity, thread: ThreadId, body: str) -> None:
        """Add a comment, changing nothing else."""
        ...

    async def mark_answered(self, as_: Identity, thread: ThreadId) -> None:
        """Label the thread answered and close it; one that is already both is
        left as it is.
        """
        ...

    async def unmark_answered(self, as_: Identity, thread: ThreadId) -> None:
        """Take the answered label off and open the thread again; one that is
        neither is left as it is.
        """
        ...

    def read_event(self, kind: str, body: bytes) -> ThreadChange | None:
        """What a pushed event of the forge's `kind` says changed among the
        threads, or none for an event about anything else or one that does
        not read. No call is made: the event was checked before it got here.
        """
        ...
