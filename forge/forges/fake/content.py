"""The content area in memory. A contest or task is made bare, as at a real
forge, and `secure` attaches its roles and its protection; a person reaches
it through an org role, or through a contest or task role once attached. A
scope's three roles are attached together, and `secure` counts each.
"""

from collections.abc import Mapping

from forge.domain.content import (
    Change,
    ConflictToken,
    EntryKind,
    File,
    Files,
    FileSet,
    TreeEntry,
)
from forge.domain.errors import Conflict, NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId
from forge.domain.roles import Role, Scope
from forge.forges.fake.state import Repo, State, token_of
from forge.forges.ids import (
    CONTEST,
    PUBLISHED_PREFIX,
    TASK,
    ContestRef,
    TaskRef,
    location,
    parse_contest,
)
from forge.port.content import ContentPlace


class FakeContent:
    def __init__(self, state: State) -> None:
        self._state = state

    async def create_contest(self, org: OrgId, key: str, files: Files) -> ContestId:
        self._state.record("create_contest", PLATFORM, org=org, key=key)
        self._state.check_up()
        self._state.org(org)
        ref = ContestRef(org, key)
        self._state.create_repo(PLATFORM, org, ref.repo, files, scope=Scope(org, key))
        return ref.id

    async def create_task(self, contest: ContestId, key: str, files: Files) -> TaskId:
        self._state.record("create_task", PLATFORM, contest=contest, key=key)
        self._state.check_up()
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, key)
        self._state.create_repo(
            PLATFORM, ref.org, ref.repo, files, scope=Scope(ref.org, ref.contest, ref.task)
        )
        return ref.id

    async def secure(self, place: ContentPlace) -> int:
        self._state.record("secure", PLATFORM, place=place)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        assert repo.scope is not None
        put_back = 0
        for scope in repo.scope.lineage()[1:]:
            if scope not in repo.teams:
                repo.teams.add(scope)
                put_back += len(Role)
        if not repo.rewrites_refused:
            repo.rewrites_refused = True
            put_back += 1
        if repo.scope.task is not None and PUBLISHED_PREFIX not in repo.reserved:
            repo.reserved.add(PUBLISHED_PREFIX)
            put_back += 1
        return put_back

    async def delete_place(self, place: ContentPlace) -> None:
        self._state.record("delete_place", PLATFORM, place=place)
        self._state.check_up()
        owner, name = location(place)
        repo = self._state.repos.pop((owner, name), None)
        org = self._state.orgs.get(owner)
        if repo is None or repo.scope is None or org is None:
            return
        for role in Role:
            org.roles.pop((repo.scope, role), None)

    async def exists(self, place: ContentPlace) -> bool:
        self._state.record("exists", PLATFORM, place=place)
        self._state.check_up()
        try:
            self._state.repo(*location(place))
        except NotFound:
            return False
        return True

    async def list_contests(self, as_: Identity, org: OrgId) -> tuple[ContestId, ...]:
        self._state.record("list_contests", as_, org=org)
        self._state.check_up()
        self._state.org(org)
        return tuple(
            ContestRef(org, repo.name.removesuffix(f".{CONTEST}")).id
            for repo in self._readable(as_, org)
            if repo.name.endswith(f".{CONTEST}")
        )

    async def list_tasks(self, as_: Identity, contest: ContestId) -> tuple[TaskId, ...]:
        self._state.record("list_tasks", as_, contest=contest)
        self._state.check_up()
        parent = parse_contest(contest)
        found = []
        for repo in self._readable(as_, parent.org):
            parts = repo.name.split(".")
            if len(parts) == 3 and parts[0] == parent.contest and parts[2] == TASK:
                found.append(TaskRef(parent.org, parent.contest, parts[1]).id)
        return tuple(found)

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId | None = None
    ) -> File:
        self._state.record("read_file", as_, place=place, path=path, at=at)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        files = self._state.files_at(repo, at)
        if path not in files:
            raise NotFound(f"{path} is not in {place}")
        return File(path=path, content=files[path], token=token_of(files[path]))

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
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_write(as_, repo)
        _check(repo, {path: expected})
        return VersionId(
            self._state.commit(repo, {path: content}, message, self._state.author(as_))
        )

    async def save_files(
        self,
        as_: Identity,
        place: ContentPlace,
        files: Mapping[str, bytes | None],
        *,
        expected: Mapping[str, ConflictToken | None],
        message: str,
    ) -> VersionId:
        self._state.record(
            "save_files", as_, place=place, paths=sorted(files), expected=dict(expected)
        )
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_write(as_, repo)
        _check(repo, {path: expected.get(path) for path in files})
        for path, content in files.items():
            if content is None and path not in repo.files:
                raise NotFound(f"{path} is not in {place}")
        return VersionId(self._state.commit(repo, files, message, self._state.author(as_)))

    async def list_files(
        self, as_: Identity, place: ContentPlace, *, at: VersionId | None = None
    ) -> FileSet:
        self._state.record("list_files", as_, place=place, at=at)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        files = self._state.files_at(repo, at)
        return FileSet(
            version=VersionId(at if at is not None else repo.head),
            tokens={path: token_of(content) for path, content in files.items()},
        )

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        self._state.record("list_tree", as_, place=place, path=path)
        self._state.check_up()
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
        if path and not seen:
            raise NotFound(f"{path} is not in {place}")
        return tuple(seen.values())

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        self._state.record("history", as_, place=place, path=path)
        self._state.check_up()
        repo = self._state.repo(*location(place))
        self._state.require_read(as_, repo)
        return tuple(
            change
            for change in reversed(repo.history)
            if path is None or path in repo.touched.get(change.version, set())
        )

    def _readable(self, as_: Identity, org: str) -> list[Repo]:
        user_id = self._state.author(as_)
        return sorted(
            (
                repo
                for (owner, _), repo in self._state.repos.items()
                if owner == org and (user_id is None or self._state.may_read(user_id, repo))
            ),
            key=lambda repo: repo.name,
        )


def _check(repo: Repo, expected: Mapping[str, ConflictToken | None]) -> None:
    """Refuse the write when any file has moved since it was read, or exists
    when it was expected not to.
    """
    for path, token in expected.items():
        current = repo.tokens.get(path)
        if token is None and current is not None:
            raise Conflict(f"{path} already exists")
        if token is not None and token != current:
            raise Conflict(f"{path} has changed since it was read")
