"""The thread area in memory."""

from dataclasses import replace

from forge.domain.errors import NotFound
from forge.domain.identity import Identity
from forge.domain.ids import ThreadId
from forge.domain.threads import Comment, Thread, ThreadKind, ThreadPlace
from forge.forges.fake.state import State
from forge.forges.ids import location, parse_thread, thread_id


class FakeThreads:
    def __init__(self, state: State) -> None:
        self._state = state

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        self._state.record("post_thread", as_, place=place, kind=kind)
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        made = thread_id(repo.owner, repo.name, len(self._state.threads) + 1)
        self._state.threads[made] = Thread(
            id=made,
            kind=kind,
            title=title,
            body=body,
            author_id=self._state.author(as_),
            created_at=self._state.clock.now(),
            closed=False,
            answered=False,
        )
        return made

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]:
        self._state.record("list_threads", as_, place=place, kind=kind)
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        prefix = f"{repo.owner}/{repo.name}#"
        return tuple(
            thread
            for existing, thread in self._state.threads.items()
            if existing.startswith(prefix) and thread.kind is kind
        )

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        self._state.record("edit_thread", as_, thread=thread)
        found = self._thread(as_, thread)
        self._state.threads[thread] = replace(found, title=title, body=body)

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        self._state.record("close_thread", as_, thread=thread)
        found = self._thread(as_, thread)
        self._state.threads[thread] = replace(found, closed=True)

    async def comment(
        self, as_: Identity, thread: ThreadId, body: str, *, answered: bool = False
    ) -> None:
        self._state.record("comment", as_, thread=thread, answered=answered)
        found = self._thread(as_, thread)
        comment = Comment(
            id=str(len(found.comments) + 1),
            author_id=self._state.author(as_),
            body=body,
            at=self._state.clock.now(),
        )
        self._state.threads[thread] = replace(
            found,
            comments=(*found.comments, comment),
            answered=found.answered or answered,
            closed=found.closed or answered,
        )

    def _thread(self, as_: Identity, thread: ThreadId) -> Thread:
        found = self._state.threads.get(thread)
        if found is None:
            raise NotFound(f"no thread {thread}")
        owner, name, _ = parse_thread(thread)
        self._state.require_read(as_, self._state.repo(owner, name))
        return found
