"""The thread area in memory."""

from dataclasses import replace

from forge.domain.errors import NotFound
from forge.domain.identity import Identity
from forge.domain.ids import ThreadId, WorkspaceId
from forge.domain.threads import Comment, Thread, ThreadKind, ThreadPlace
from forge.forges.fake import ids
from forge.forges.fake.content import content_repo
from forge.forges.fake.state import Repo, State


class FakeThreads:
    def __init__(self, state: State) -> None:
        self._state = state

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> ThreadId:
        self._state.record("post_thread", as_, place=place, kind=kind)
        repo = self._thread_repo(place)
        self._state.require_read(as_, repo)
        thread_id = ThreadId(f"{repo.owner}/{repo.name}#{len(self._state.threads) + 1}")
        self._state.threads[thread_id] = Thread(
            id=thread_id,
            kind=kind,
            title=title,
            body=body,
            author_id=self._state.author(as_),
            created_at=self._state.clock.now(),
            closed=False,
            answered=False,
        )
        return thread_id

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind
    ) -> tuple[Thread, ...]:
        self._state.record("list_threads", as_, place=place, kind=kind)
        repo = self._thread_repo(place)
        self._state.require_read(as_, repo)
        prefix = f"{repo.owner}/{repo.name}#"
        return tuple(
            thread
            for thread_id, thread in self._state.threads.items()
            if thread_id.startswith(prefix) and thread.kind is kind
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

    def _thread_repo(self, place: ThreadPlace) -> Repo:
        if ids.is_workspace(place):
            org, _, segment = ids.workspace_parts(WorkspaceId(place))
            return self._state.repo(org, ids.desk_repo(segment))
        return content_repo(self._state, place)

    def _thread(self, as_: Identity, thread: ThreadId) -> Thread:
        found = self._state.threads.get(thread)
        if found is None:
            raise NotFound(f"no thread {thread}")
        owner, name = thread.rsplit("#", 1)[0].split("/")
        self._state.require_read(as_, self._state.repo(owner, name))
        return found
