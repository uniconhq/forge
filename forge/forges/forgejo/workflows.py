"""The workflow area over Forgejo. A workflow is a repository carrying the
workflow topic: private, private with read collaborators for shared, or
public. A version is a tag, and a copy is a new repository made from the
source tree at a version.
"""

from typing import Any

from forge.domain.content import File, Files
from forge.domain.errors import Forbidden
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import WorkflowId
from forge.domain.workflows import Visibility, Workflow
from forge.forges.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.forges.forgejo.users import Users
from forge.forges.ids import PROTECTED_PREFIXES, WORKFLOW, WorkflowRef, parse_workflow

READ = "read"
WORKFLOW_TOPIC = "unicon-workflow"


class ForgejoWorkflows:
    def __init__(self, repos: Repos, users: Users) -> None:
        self._repos = repos
        self._users = users

    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        ref = WorkflowRef(owner, name)
        await self._repos.create(
            as_, ref.owner, ref.repo, files, private=visibility is not Visibility.PUBLIC
        )
        await self._repos.protect_branch(ref.owner, ref.repo)
        await self._repos.mark(as_, ref.owner, ref.repo, WORKFLOW_TOPIC)
        return ref.id

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        ref = parse_workflow(workflow)
        await self._repos.set_private(
            as_, ref.owner, ref.repo, private=visibility is not Visibility.PUBLIC
        )

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        username = await self._users.username_of(user_id)
        await self._repos.add_collaborator(as_, ref.owner, ref.repo, username, permission=READ)

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        username = await self._users.username_of(user_id)
        await self._repos.remove_collaborator(as_, ref.owner, ref.repo, username)

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str
    ) -> None:
        if version.startswith(PROTECTED_PREFIXES):
            raise Forbidden(f"{version} is reserved for protected versions")
        ref = parse_workflow(workflow)
        await self._repos.create_version(as_, ref.owner, ref.repo, version, DEFAULT_BRANCH)

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        ref = parse_workflow(workflow)
        return await self._repos.read_file(as_, ref.owner, ref.repo, path, at=version)

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
        await self._repos.star(as_, ref.owner, ref.repo)

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        origin = parse_workflow(source)
        files = await self._repos.files_at(as_, origin.owner, origin.repo, version)
        return await self.create_workflow(as_, owner, name, files, Visibility.PRIVATE)

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
    return str(repo["name"]).endswith(f".{WORKFLOW}")
