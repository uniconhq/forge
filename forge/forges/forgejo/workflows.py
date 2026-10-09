"""The workflow area over Forgejo. A workflow is a repository carrying the
workflow topic: private, private with read collaborators for shared, or
public. A version is a tag, and a copy is a new repository made from the
source tree at a version.

Only the platform may create a repository at the forge, so the platform
makes the workflow's: under the person's own name through the administrator's
endpoint, which leaves them its owner, or in the org, where the org's three
role teams already cover every repository, admin and manager with `write`
and observer with `read`. The platform protects `main` and sets the mark;
the person writes the first commit, so it is theirs. Changing who may read a
workflow needs a repository admin, which no organiser's team is, so it is
done as the platform once the forge has said, to the person's own
credential, that they may write the workflow.
"""

from typing import Any

from forge.domain.content import ConflictToken, File, Files
from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import PLATFORM, Identity, Platform, User
from forge.domain.ids import VersionId, WorkflowId
from forge.domain.workflows import Visibility, Workflow
from forge.forges.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.forges.forgejo.users import Users
from forge.forges.ids import (
    PLATFORM_ORG,
    PROTECTED_PREFIXES,
    WORKFLOW,
    WorkflowRef,
    parse_workflow,
)

READ = "read"
WORKFLOW_TOPIC = "unicon-workflow"


class ForgejoWorkflows:
    def __init__(self, repos: Repos, users: Users) -> None:
        self._repos = repos
        self._users = users

    async def workflow_key(self, workflow: WorkflowId) -> str:
        ref = parse_workflow(workflow)
        return str((await self._repos.record(ref.owner, ref.repo))["id"])

    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        ref = WorkflowRef(owner, name)
        try:
            await self._repos.create(
                as_, ref.owner, ref.repo, files, private=visibility is not Visibility.PUBLIC
            )
        except Conflict:
            # Only the platform makes repositories, so one of the name without
            # the mark is a create that stopped before the end: it is finished,
            # so the name it holds opens as the workflow it was to be.
            if not _is_workflow(await self._repos.record(ref.owner, ref.repo)):
                await self._finish(ref)
            raise
        await self._finish(ref)
        return ref.id

    async def _finish(self, ref: WorkflowRef) -> None:
        await self._repos.protect_branch(ref.owner, ref.repo)
        await self._repos.mark(ref.owner, ref.repo, WORKFLOW_TOPIC)

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        ref = parse_workflow(workflow)
        await self._require_writer(as_, ref)
        await self._repos.set_private(
            ref.owner, ref.repo, private=visibility is not Visibility.PUBLIC
        )

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        await self._require_writer(as_, ref)
        username = await self._users.username_of(user_id)
        await self._repos.add_collaborator(ref.owner, ref.repo, username, permission=READ)

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        await self._require_writer(as_, ref)
        username = await self._users.username_of(user_id)
        await self._repos.remove_collaborator(ref.owner, ref.repo, username)

    async def workflow_readers(self, as_: Identity, workflow: WorkflowId) -> tuple[User, ...]:
        ref = parse_workflow(workflow)
        await self._require_writer(as_, ref)
        readers = await self._repos.collaborators(ref.owner, ref.repo)
        return tuple(
            User(id=int(reader["id"]), username=str(reader["login"]))
            for reader in sorted(readers, key=lambda reader: str(reader["login"]).lower())
        )

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str, at: VersionId | None = None
    ) -> None:
        if version.startswith(PROTECTED_PREFIXES):
            raise Forbidden(f"{version} is reserved for protected versions")
        ref = parse_workflow(workflow)
        await self._require_mark(as_, ref)
        await self._repos.create_version(
            as_, ref.owner, ref.repo, version, str(at) if at is not None else DEFAULT_BRANCH
        )

    async def read_workflow_draft(
        self, as_: Identity, workflow: WorkflowId, path: str
    ) -> tuple[VersionId, File]:
        ref = parse_workflow(workflow)
        await self._require_mark(as_, ref)
        head = await self._repos.head(as_, ref.owner, ref.repo)
        return VersionId(head), await self._repos.read_file(as_, ref.owner, ref.repo, path, at=head)

    async def write_workflow_file(
        self,
        as_: Identity,
        workflow: WorkflowId,
        path: str,
        content: bytes,
        *,
        expected: ConflictToken | None,
        message: str,
    ) -> File:
        ref = parse_workflow(workflow)
        await self._require_mark(as_, ref)
        version = await self._repos.commit_files(
            as_, ref.owner, ref.repo, {path: content}, expected={path: expected}, message=message
        )
        return await self._repos.read_file(as_, ref.owner, ref.repo, path, at=str(version))

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        ref = parse_workflow(workflow)
        await self._require_mark(as_, ref)
        at = await self._repos.require_version(as_, ref.owner, ref.repo, version)
        return await self._repos.read_file(as_, ref.owner, ref.repo, path, at=at)

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]:
        found = await self._repos.marked(WORKFLOW_TOPIC)
        public = [
            repo
            for repo in found
            if _is_workflow(repo) and not repo.get("private") and query in str(repo["name"])
        ]
        return tuple([await self._workflow(repo) for repo in public])

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None:
        ref = parse_workflow(workflow)
        await self._require_mark(as_, ref)
        await self._repos.star(as_, ref.owner, ref.repo)

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        origin = parse_workflow(source)
        await self._require_mark(as_, origin)
        at = await self._repos.require_version(as_, origin.owner, origin.repo, version)
        files = await self._repos.files_at(as_, origin.owner, origin.repo, at)
        return await self.create_workflow(as_, owner, name, files, Visibility.PRIVATE)

    async def describe_workflow(self, as_: Identity, workflow: WorkflowId) -> Workflow:
        ref = parse_workflow(workflow)
        return await self._workflow(await self._require_mark(as_, ref))

    async def workflows_readable_by(self, as_: Identity) -> tuple[Workflow, ...]:
        """Found by the mark, so the cost follows the workflows, not every
        repository `as_` reaches: the private ones `as_` sees, which they
        reach as owner, collaborator or through an org's team, and the public
        ones of `as_`, of each org they belong to, and the platform's.
        """
        private = await self._repos.marked(WORKFLOW_TOPIC, as_, is_private="true")
        public: list[dict[str, Any]] = []
        for owner_id in await self._owners_of(as_):
            public.extend(
                await self._repos.marked(
                    WORKFLOW_TOPIC, as_, uid=owner_id, exclusive="true", is_private="false"
                )
            )
        seen: set[str] = set()
        found: list[Workflow] = []
        for repo in [*private, *public]:
            key = str(repo["full_name"])
            if key in seen or not _is_workflow(repo):
                continue
            seen.add(key)
            if repo["owner"]["login"] == PLATFORM_ORG and repo.get("private"):
                continue
            found.append(await self._workflow(repo))
        return tuple(found)

    async def _owners_of(self, as_: Identity) -> list[int]:
        """The forge's numbers for `as_`, each org they belong to, and the
        platform's org, whose public workflows they reach.
        """
        owners = [await self._repos.org_id(PLATFORM_ORG)]
        if isinstance(as_, Platform):
            return owners
        me = await self._users.me(as_)
        orgs = await self._users.orgs_of(as_)
        return [int(me["id"]), *(int(org["id"]) for org in orgs), *owners]

    async def _require_mark(self, as_: Identity, ref: WorkflowRef) -> dict[str, Any]:
        """The workflow's repository as `as_` sees it; `NotFound` for one they
        may not read or one without the workflow mark, alike.
        """
        record = await self._repos.seen_by(as_, ref.owner, ref.repo)
        if not _is_workflow(record):
            raise NotFound(f"{ref.owner}/{ref.repo} is not a workflow")
        return record

    async def _require_writer(self, as_: Identity, ref: WorkflowRef) -> None:
        """Refuse as `Forbidden` unless the forge says, to `as_`'s own
        credential, that they may write the workflow, for a change the
        platform then makes in their name; `NotFound` as `_require_mark`.
        """
        record = await self._require_mark(as_, ref)
        if not isinstance(as_, Platform) and not (record.get("permissions") or {}).get("push"):
            raise Forbidden(f"the caller may not change {ref.owner}/{ref.repo}")

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        username = await self._users.username_of(user_id)
        owned = [repo for repo in await self._repos.owned_by(username) if _is_workflow(repo)]
        return tuple([await self._workflow(repo) for repo in owned])

    async def _workflow(self, repo: dict[str, Any]) -> Workflow:
        owner = str(repo["owner"]["login"])
        name = str(repo["name"]).removesuffix(f".{WORKFLOW}")
        return Workflow(
            id=WorkflowRef(owner, name).id,
            owner=owner,
            name=name,
            visibility=await self._visibility(repo),
            stars=int(repo.get("stars_count") or 0),
            versions=tuple(await self._repos.versions(PLATFORM, owner, str(repo["name"]))),
        )

    async def _visibility(self, repo: dict[str, Any]) -> Visibility:
        if not repo.get("private"):
            return Visibility.PUBLIC
        readers = await self._repos.collaborators(str(repo["owner"]["login"]), str(repo["name"]))
        return Visibility.SHARED if readers else Visibility.PRIVATE


def _is_workflow(repo: dict[str, Any]) -> bool:
    """Whether a repository is a workflow: named as one and carrying the
    workflow mark, which a create sets last, so one a create left unmarked
    is not one until a create of its name finishes it.
    """
    marked = WORKFLOW_TOPIC in (repo.get("topics") or [])
    return marked and str(repo["name"]).endswith(f".{WORKFLOW}")
