"""The content area over Forgejo: contest and task repositories under their
org, made bare and then secured, read and written through the contents API as
the calling identity. Securing a repository attaches the role teams of its
contest and, for a task, of the task itself, refuses force-pushes to its
default branch, and for a task reserves the `published/` tags for the
platform account; each is checked first and only what is missing is made.
Deleting one removes its own role teams and then the repository.
"""

from collections.abc import Mapping

from forge.domain.content import Change, ConflictToken, File, Files, FileSet, TreeEntry
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId
from forge.domain.roles import Scope
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.ids import (
    CONTEST,
    PUBLISHED_PREFIX,
    TASK,
    ContestRef,
    TaskRef,
    location,
    parse_contest,
    parse_task,
)
from forge.port.content import ContentPlace


class ForgejoContent:
    def __init__(self, repos: Repos, teams: Teams) -> None:
        self._repos = repos
        self._teams = teams

    async def create_contest(self, org: OrgId, key: str, files: Files) -> ContestId:
        ref = ContestRef(org, key)
        await self._repos.create(PLATFORM, org, ref.repo, files, private=True)
        return ref.id

    async def create_task(self, contest: ContestId, key: str, files: Files) -> TaskId:
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, key)
        await self._repos.create(PLATFORM, parent.org, ref.repo, files, private=True)
        return ref.id

    async def secure(self, place: ContentPlace) -> int:
        owner, name = location(place)
        scope = _scope(place)
        put_back = await self._teams.attach_scope(scope, name)
        put_back += await self._repos.protect_branch(owner, name)
        if scope.task is not None:
            put_back += await self._repos.reserve_versions(owner, name, PUBLISHED_PREFIX)
        return put_back

    async def delete_place(self, place: ContentPlace) -> None:
        """The place's own role teams first, the reverse of the order they
        were made in, then its repository, which takes its protections and
        its attachment to the teams of the contest above it.
        """
        owner, name = location(place)
        await self._teams.delete_scope(_scope(place))
        await self._repos.delete(owner, name)

    async def exists(self, place: ContentPlace) -> bool:
        return await self._repos.exists(*location(place))

    async def list_contests(self, as_: Identity, org: OrgId) -> tuple[ContestId, ...]:
        names = sorted(
            str(repo["name"]) for repo in await self._repos.named_with(org, f".{CONTEST}", as_)
        )
        return tuple(
            ContestRef(org, name.removesuffix(f".{CONTEST}")).id
            for name in names
            if name.endswith(f".{CONTEST}") and name.count(".") == 1
        )

    async def list_tasks(self, as_: Identity, contest: ContestId) -> tuple[TaskId, ...]:
        parent = parse_contest(contest)
        found = []
        repos = await self._repos.named_with(parent.org, f".{TASK}", as_)
        for repo in sorted(str(repo["name"]) for repo in repos):
            parts = repo.split(".")
            if len(parts) == 3 and parts[0] == parent.contest and parts[2] == TASK:
                found.append(TaskRef(parent.org, parent.contest, parts[1]).id)
        return tuple(found)

    async def read_file(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId | None = None
    ) -> File:
        owner, name = location(place)
        return await self._repos.read_file(as_, owner, name, path, at=at)

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
        owner, name = location(place)
        return await self._repos.write_file(
            as_, owner, name, path, content, message=message, expected=expected
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
        owner, name = location(place)
        return await self._repos.commit_files(
            as_, owner, name, files, expected=expected, message=message
        )

    async def list_files(
        self, as_: Identity, place: ContentPlace, *, at: VersionId | None = None
    ) -> FileSet:
        owner, name = location(place)
        return await self._repos.file_set(as_, owner, name, at)

    async def list_tree(
        self, as_: Identity, place: ContentPlace, path: str = ""
    ) -> tuple[TreeEntry, ...]:
        owner, name = location(place)
        return await self._repos.tree(as_, owner, name, path)

    async def history(
        self, as_: Identity, place: ContentPlace, path: str | None = None
    ) -> tuple[Change, ...]:
        owner, name = location(place)
        return await self._repos.history(as_, owner, name, path)


def _scope(place: ContentPlace) -> Scope:
    if place.count("/") == 1:
        contest = parse_contest(ContestId(place))
        return Scope(contest.org, contest.contest)
    task = parse_task(TaskId(place))
    return Scope(task.org, task.contest, task.task)
