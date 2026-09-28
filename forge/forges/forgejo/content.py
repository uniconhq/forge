"""The content area over Forgejo: contest and task repositories under their
org, attached to their scope's role teams, read and written through the
contents API as the calling identity.
"""

from forge.domain.content import Change, ConflictToken, File, Files, TreeEntry
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, OrgName, TaskId, VersionId
from forge.domain.roles import Scope
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    ContestRef,
    TaskRef,
    location,
    parse_contest,
)
from forge.port.content import ContentPlace


class ForgejoContent:
    def __init__(self, repos: Repos, teams: Teams) -> None:
        self._repos = repos
        self._teams = teams

    async def create_contest(self, org: OrgName, name: str, files: Files) -> ContestId:
        ref = ContestRef(org, name)
        await self._repos.create(PLATFORM, org, ref.repo, files, private=True)
        await self._teams.attach_scope(Scope(org, name), ref.repo)
        return ref.id

    async def create_task(self, contest: ContestId, name: str, files: Files) -> TaskId:
        parent = parse_contest(contest)
        ref = TaskRef(parent.org, parent.contest, name)
        await self._repos.create(PLATFORM, parent.org, ref.repo, files, private=True)
        await self._teams.attach_scope(Scope(parent.org, parent.contest, name), ref.repo)
        await self._repos.protect_versions(parent.org, ref.repo, PUBLISHED_PREFIX)
        return ref.id

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
