"""The content area in memory."""

from forge.domain.content import Change, ConflictToken, EntryKind, File, Files, TreeEntry
from forge.domain.errors import Conflict, NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, OrgName, TaskId, VersionId
from forge.domain.roles import Scope
from forge.forges.fake.state import State
from forge.forges.ids import ContestRef, TaskRef, location, parse_contest
from forge.port.content import ContentPlace


class FakeContent:
    def __init__(self, state: State) -> None:
        self._state = state

    async def create_contest(self, org: OrgName, name: str, files: Files) -> ContestId:
        self._state.record("create_contest", PLATFORM, org=org, name=name)
        self._state.org(org)
        ref = ContestRef(org, name)
        self._state.create_repo(org, ref.repo, files, scope=Scope(org, name))
        return ref.id

    async def create_task(self, contest: ContestId, name: str, files: Files) -> TaskId:
        self._state.record("create_task", PLATFORM, contest=contest, name=name)
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, name)
        self._state.create_repo(
            ref.org, ref.repo, files, scope=Scope(ref.org, ref.contest, ref.task)
        )
        return ref.id

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId | None = None
    ) -> File:
        self._state.record("read_file", as_, place=place, path=path, at=at)
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        if at is not None and at not in {change.version for change in repo.history}:
            raise NotFound(f"{place} has no version {at}")
        if path not in repo.files:
            raise NotFound(f"{path} is not in {place}")
        return File(path=path, content=repo.files[path], token=repo.tokens[path])

    async def write_file(
        self,
        as_: Identity,
        place: ContentPlace,
        path: str,
        content: bytes,
        *,
        message: str,
        expected: ConflictToken | None,
    ) -> VersionId:
        self._state.record("write_file", as_, place=place, path=path, expected=expected)
        repo = self._state.repo(*location(place))
        self._state.require_write(as_, repo)
        current = repo.tokens.get(path)
        if expected is None and current is not None:
            raise Conflict(f"{path} already exists")
        if expected is not None and expected != current:
            raise Conflict(f"{path} has changed since it was read")
        return VersionId(
            self._state.commit(repo, {path: content}, message, self._state.author(as_))
        )

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        self._state.record("list_tree", as_, place=place, path=path)
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        prefix = f"{path.rstrip('/')}/" if path else ""
        seen: dict[str, TreeEntry] = {}
        for file_path in sorted(repo.files):
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            if "/" in rest:
                directory = prefix + rest.split("/", 1)[0]
                seen.setdefault(directory, TreeEntry(directory, EntryKind.DIRECTORY))
            else:
                seen[file_path] = TreeEntry(file_path, EntryKind.FILE, len(repo.files[file_path]))
        return tuple(seen.values())

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        self._state.record("history", as_, place=place, path=path)
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        return tuple(reversed(repo.history))
