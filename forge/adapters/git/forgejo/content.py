"""The content area over Forgejo: contest and task repositories under their
org, made bare and then secured, read through the contents API as the
calling identity and written through it as the platform, with the organiser
who saved as the commit's author and committer. Securing a repository
attaches the role teams of its contest and, for a task, of the task itself,
protects its default branch so only the platform's account may push or merge
to it and nobody may force-push, and for a task reserves the `published/`
tags for the platform account; each is checked first and only what is
missing is made or put back.
Deleting one removes its own role teams and then the repository, and makes
the teams again when the repository stays.
"""

from collections.abc import Collection, Mapping

from forge.adapters.git.forgejo.repos import Repos
from forge.adapters.git.forgejo.teams import Teams
from forge.adapters.git.forgejo.users import Users
from forge.adapters.ids import (
    CONTEST,
    DISPATCH_FILES,
    DISPATCH_REPO,
    PUBLISHED_PREFIX,
    TASK,
    ContestRef,
    TaskRef,
    location,
    parse_contest,
    parse_task,
)
from forge.domain.content import Change, ConflictToken, File, Files, FileSet, TreeEntry
from forge.domain.errors import Conflict, PortError
from forge.domain.identity import PLATFORM, Identity, User
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId
from forge.domain.roles import Scope
from forge.domain.uploads import Door
from forge.port.content import ContentPlace


class ForgejoContent:
    def __init__(self, repos: Repos, teams: Teams, users: Users) -> None:
        self._repos = repos
        self._teams = teams
        self._users = users

    async def create_contest(self, org: OrgId, key: str, files: Files) -> ContestId:
        ref = ContestRef(org, key)
        await self._repos.create(PLATFORM, org, ref.repo, files, private=True)
        return ref.id

    async def create_task(self, contest: ContestId, key: str, files: Files) -> TaskId:
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, key)
        await self._repos.create(PLATFORM, parent.org, ref.repo, files, private=True)
        return ref.id

    async def create_dispatch(self, org: OrgId) -> None:
        try:
            await self._repos.create(PLATFORM, org, DISPATCH_REPO, DISPATCH_FILES, private=True)
        except Conflict:
            return

    async def secure(self, place: ContentPlace) -> int:
        owner, name = location(place)
        scope = _scope(place)
        put_back = await self._teams.attach_scope(scope, name)
        put_back += await self._repos.protect_branch(owner, name, platform_only=True)
        if scope.task is not None:
            put_back += await self._repos.reserve_versions(owner, name, PUBLISHED_PREFIX)
        return put_back

    async def delete_place(self, place: ContentPlace) -> None:
        """The place's own role teams first, the reverse of the order they
        were made in, then its repository, which takes its protections and
        its attachment to the teams of the contest above it. When the
        repository is not deleted, its teams are made again with the members
        they had, so a removal that fails gives the place back with its role
        holders.
        """
        owner, name = location(place)
        scope = _scope(place)
        held = await self._teams.scope_members(scope)
        await self._teams.delete_scope(scope)
        try:
            await self._repos.delete(owner, name)
        except PortError:
            await self._teams.restore_scope(scope, name, held)
            raise

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

    async def read_media(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId, max_size: int
    ) -> bytes:
        owner, name = location(place)
        return await self._repos.read_raw(as_, owner, name, path, at=at, max_size=max_size)

    async def media_door(
        self, as_: Identity, place: ContentPlace, path: str, *, at: VersionId
    ) -> Door:
        owner, name = location(place)
        return await self._repos.media_door(as_, owner, name, path, at=at)

    async def write_file(
        self,
        as_: Identity,
        place: ContentPlace,
        path: str,
        content: bytes,
        *,
        message: str,
        expected: ConflictToken | None,
        author: int | None = None,
    ) -> VersionId:
        owner, name = location(place)
        return await self._repos.write_file(
            as_,
            owner,
            name,
            path,
            content,
            message=message,
            expected=expected,
            author=await self._author(author),
        )

    async def save_files(
        self,
        as_: Identity,
        place: ContentPlace,
        files: Mapping[str, bytes | None],
        *,
        expected: Mapping[str, ConflictToken | None],
        message: str,
        author: int | None = None,
    ) -> VersionId:
        owner, name = location(place)
        return await self._repos.commit_files(
            as_,
            owner,
            name,
            files,
            expected=expected,
            message=message,
            author=await self._author(author),
        )

    async def _author(self, user_id: int | None) -> User | None:
        """The person a change the platform writes names as its author, read
        as the platform.
        """
        return await self._users.find(user_id) if user_id is not None else None

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

    async def read_blobs(
        self, as_: Identity, place: ContentPlace, tokens: Collection[ConflictToken]
    ) -> Mapping[ConflictToken, bytes]:
        owner, name = location(place)
        return await self._repos.blobs(as_, owner, name, tokens)

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
