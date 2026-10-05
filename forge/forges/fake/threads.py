"""The thread area in memory. Threads are numbered within their repository,
as the forge numbers issues; posting, editing, closing, marking and
unmarking need write access to the place and commenting needs read access,
as at the forge. An event is read from the same shape Forgejo pushes, the
repository's owner and name and the issue's number, so a test can hand the
fake what the real forge would send.
"""

from dataclasses import replace

from forge.domain.errors import Forbidden, NotFound
from forge.domain.identity import Identity
from forge.domain.ids import OrgId, ThreadId
from forge.domain.threads import Comment, Thread, ThreadChange, ThreadKind, ThreadPlace
from forge.forges.fake.state import State
from forge.forges.ids import location, parse_thread, thread_id, thread_pushed


class FakeThreads:
    def __init__(self, state: State) -> None:
        self._state = state

    def thread_of(self, place: ThreadPlace, number: int) -> ThreadId:
        org, repo = location(place)
        return thread_id(org, repo, number)

    async def post_thread(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, title: str, body: str
    ) -> Thread:
        self._state.record("post_thread", as_, place=place, kind=kind)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        prefix = f"{repo.owner}/{repo.name}#"
        number = 1 + sum(1 for existing in self._state.threads if existing.startswith(prefix))
        made = Thread(
            id=thread_id(repo.owner, repo.name, number),
            place=place,
            number=number,
            kind=kind,
            title=title,
            body=body,
            author_id=self._state.author(as_),
            created_at=self._state.clock.now(),
            closed=False,
            answered=False,
        )
        self._state.threads[made.id] = made
        return made

    async def list_threads(
        self, as_: Identity, place: ThreadPlace, kind: ThreadKind, *, comments: bool = True
    ) -> tuple[Thread, ...]:
        self._state.record("list_threads", as_, place=place, kind=kind, comments=comments)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        prefix = f"{repo.owner}/{repo.name}#"
        return tuple(
            sorted(
                (
                    thread
                    for existing, thread in self._state.threads.items()
                    if existing.startswith(prefix) and thread.kind is kind
                ),
                key=lambda thread: thread.number,
            )
        )

    async def read_thread(self, as_: Identity, thread: ThreadId) -> Thread:
        self._state.record("read_thread", as_, thread=thread)
        self._state.check_up()
        try:
            return self._thread(as_, thread)
        except Forbidden:
            raise NotFound(f"no thread {thread}") from None

    async def search_threads(
        self,
        as_: Identity,
        org: OrgId,
        kind: ThreadKind,
        *,
        open_only: bool = True,
        comments: bool = True,
    ) -> tuple[Thread, ...]:
        self._state.record("search_threads", as_, org=org, kind=kind, open_only=open_only)
        self._state.check_up()
        user_id = self._state.author(as_)
        found = []
        for existing, thread in self._state.threads.items():
            owner, name, _ = parse_thread(existing)
            if owner != org or thread.kind is not kind or (open_only and thread.closed):
                continue
            if user_id is None or self._state.may_read(user_id, self._state.repo(owner, name)):
                found.append(thread if comments else replace(thread, comments=()))
        return tuple(sorted(found, key=lambda thread: thread.created_at))

    async def edit_thread(self, as_: Identity, thread: ThreadId, *, title: str, body: str) -> None:
        self._state.record("edit_thread", as_, thread=thread)
        found = self._written(as_, thread)
        self._state.threads[thread] = replace(found, title=title, body=body)

    async def close_thread(self, as_: Identity, thread: ThreadId) -> None:
        self._state.record("close_thread", as_, thread=thread)
        found = self._written(as_, thread)
        self._state.threads[thread] = replace(found, closed=True)

    async def comment(self, as_: Identity, thread: ThreadId, body: str) -> None:
        self._state.record("comment", as_, thread=thread)
        self._state.check_up()
        found = self._thread(as_, thread)
        comment = Comment(
            id=str(len(found.comments) + 1),
            author_id=self._state.author(as_),
            body=body,
            at=self._state.clock.now(),
        )
        self._state.threads[thread] = replace(found, comments=(*found.comments, comment))

    async def mark_answered(self, as_: Identity, thread: ThreadId) -> None:
        self._state.record("mark_answered", as_, thread=thread)
        found = self._written(as_, thread)
        self._state.threads[thread] = replace(found, answered=True, closed=True)

    async def unmark_answered(self, as_: Identity, thread: ThreadId) -> None:
        self._state.record("unmark_answered", as_, thread=thread)
        found = self._written(as_, thread)
        self._state.threads[thread] = replace(found, answered=False, closed=False)

    def read_event(self, kind: str, body: bytes) -> ThreadChange | None:
        return thread_pushed(kind, body)

    def _thread(self, as_: Identity, thread: ThreadId) -> Thread:
        found = self._state.threads.get(thread)
        if found is None:
            raise NotFound(f"no thread {thread}")
        owner, name, _ = parse_thread(thread)
        self._state.require_read(as_, self._state.repo(owner, name))
        return found

    def _written(self, as_: Identity, thread: ThreadId) -> Thread:
        self._state.check_up()
        found = self._thread(as_, thread)
        owner, name, _ = parse_thread(thread)
        self._state.require_write(as_, self._state.repo(owner, name))
        return found
