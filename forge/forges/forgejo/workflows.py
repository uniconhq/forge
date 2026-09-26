"""Workflows and primitives at Forgejo. Both are repositories carrying a
topic that says which they are. A workflow's visibility is the repository's:
private, private with read collaborators for shared, or public. A version is
a tag, and a copy is a new repository made from the source tree at a version.
"""

import base64
from typing import Any

from forge.domain.content import File, Files
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, AsUser, Identity
from forge.domain.ids import PrimitiveId, VersionId, WorkflowId
from forge.domain.workflows import Primitive, Visibility, Workflow
from forge.forges.forgejo.base import ForgejoBase, json_of, list_of
from forge.forges.forgejo.names import (
    DEFAULT_BRANCH,
    PLATFORM_ORG,
    PRIMITIVE,
    PRIMITIVE_TOPIC,
    WORKFLOW,
    WORKFLOW_TOPIC,
    WorkflowRef,
    parse_workflow,
    primitive_repo,
)

READ_PERMISSION = "read"
DECLARATION = "primitive.yaml"
COPY_MESSAGE = "Copy"


class WorkflowOps(ForgejoBase):
    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        ref = WorkflowRef(owner, name)
        await self._create_marked_repo(as_, ref, private=visibility is not Visibility.PUBLIC)
        await self._write_all(as_, ref.owner, ref.repo, files, message="Create")
        return ref.id

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        ref = parse_workflow(workflow)
        await self._http.call(
            as_,
            "PATCH",
            f"/api/v1/repos/{ref.owner}/{ref.repo}",
            json={"private": visibility is not Visibility.PUBLIC},
        )

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        username = await self._username(user_id)
        await self._http.call(
            as_,
            "PUT",
            f"/api/v1/repos/{ref.owner}/{ref.repo}/collaborators/{username}",
            json={"permission": READ_PERMISSION},
        )

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        ref = parse_workflow(workflow)
        username = await self._username(user_id)
        await self._http.call(
            as_, "DELETE", f"/api/v1/repos/{ref.owner}/{ref.repo}/collaborators/{username}"
        )

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str
    ) -> None:
        ref = parse_workflow(workflow)
        await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{ref.owner}/{ref.repo}/tags",
            json={"tag_name": version, "target": DEFAULT_BRANCH},
        )

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        ref = parse_workflow(workflow)
        return await self._read(as_, ref.owner, ref.repo, path, version)

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]:
        found = json_of(
            await self._http.call(
                PLATFORM,
                "GET",
                "/api/v1/repos/search",
                params={"q": WORKFLOW_TOPIC, "topic": "true", "limit": 50},
            )
        )
        repos = [
            repo
            for repo in found.get("data") or []
            if _is_workflow(repo) and not repo.get("private") and query in str(repo["name"])
        ]
        return tuple([await self._workflow(repo) for repo in repos])

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None:
        ref = parse_workflow(workflow)
        await self._http.call(as_, "PUT", f"/api/v1/user/starred/{ref.owner}/{ref.repo}")

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        origin = parse_workflow(source)
        files = await self._files_at(as_, origin.owner, origin.repo, version)
        ref = WorkflowRef(owner, name)
        await self._create_marked_repo(as_, ref, private=True)
        await self._write_all(as_, ref.owner, ref.repo, files, message=COPY_MESSAGE)
        return ref.id

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        username = await self._username(user_id)
        repos = await self._http.get_all(PLATFORM, f"/api/v1/users/{username}/repos")
        return tuple([await self._workflow(repo) for repo in repos if _is_workflow(repo)])

    async def list_primitives(self) -> tuple[Primitive, ...]:
        repos = await self._http.get_all(PLATFORM, f"/api/v1/orgs/{PLATFORM_ORG}/repos")
        primitives = []
        for repo in repos:
            name = str(repo["name"])
            if not name.endswith(f".{PRIMITIVE}"):
                continue
            versions = await self._versions(PLATFORM_ORG, name)
            primitives.append(
                Primitive(
                    id=PrimitiveId(name.removesuffix(f".{PRIMITIVE}")),
                    name=name.removesuffix(f".{PRIMITIVE}"),
                    versions=versions,
                )
            )
        return tuple(primitives)

    async def read_primitive_declaration(self, primitive: PrimitiveId, version: str) -> bytes:
        found = await self._read(
            PLATFORM, PLATFORM_ORG, primitive_repo(primitive), DECLARATION, version
        )
        return found.content

    async def _create_marked_repo(self, as_: Identity, ref: WorkflowRef, *, private: bool) -> None:
        body = {
            "name": ref.repo,
            "private": private,
            "auto_init": False,
            "default_branch": DEFAULT_BRANCH,
        }
        if isinstance(as_, AsUser) and await self._username(as_.user_id) == ref.owner:
            await self._http.call(as_, "POST", "/api/v1/user/repos", json=body)
        else:
            await self._http.call(as_, "POST", f"/api/v1/orgs/{ref.owner}/repos", json=body)
        await self._http.call(
            as_, "PUT", f"/api/v1/repos/{ref.owner}/{ref.repo}/topics/{WORKFLOW_TOPIC}"
        )

    async def _write_all(
        self, as_: Identity, owner: str, repo: str, files: Files, *, message: str
    ) -> None:
        if not files:
            return
        await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{owner}/{repo}/contents",
            json={
                "branch": DEFAULT_BRANCH,
                "new_branch": DEFAULT_BRANCH,
                "message": message,
                "files": [
                    {
                        "operation": "create",
                        "path": path,
                        "content": base64.b64encode(content).decode(),
                    }
                    for path, content in sorted(files.items())
                ],
            },
        )

    async def _read(self, as_: Identity, owner: str, repo: str, path: str, version: str) -> File:
        entry = json_of(
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{owner}/{repo}/contents/{path}", params={"ref": version}
            )
        )
        return File(
            path=path,
            content=base64.b64decode(entry.get("content") or ""),
            version=VersionId(str(entry["sha"])),
        )

    async def _files_at(self, as_: Identity, owner: str, repo: str, version: str) -> Files:
        tree = json_of(
            await self._http.call(
                as_,
                "GET",
                f"/api/v1/repos/{owner}/{repo}/git/trees/{version}",
                params={"recursive": "true", "per_page": 1000},
            )
        )
        files: Files = {}
        for entry in tree.get("tree") or []:
            if entry.get("type") == "blob":
                path = str(entry["path"])
                files[path] = (await self._read(as_, owner, repo, path, version)).content
        return files

    async def _workflow(self, repo: dict[str, Any]) -> Workflow:
        owner = str(repo["owner"]["login"])
        name = str(repo["name"]).removesuffix(f".{WORKFLOW}")
        return Workflow(
            id=WorkflowRef(owner, name).id,
            owner=owner,
            name=name,
            visibility=await self._visibility(repo),
            stars=int(repo.get("stars_count") or 0),
            versions=await self._versions(owner, str(repo["name"])),
        )

    async def _visibility(self, repo: dict[str, Any]) -> Visibility:
        if not repo.get("private"):
            return Visibility.PUBLIC
        collaborators = list_of(
            await self._http.call(
                PLATFORM,
                "GET",
                f"/api/v1/repos/{repo['owner']['login']}/{repo['name']}/collaborators",
            )
        )
        return Visibility.SHARED if collaborators else Visibility.PRIVATE

    async def _versions(self, owner: str, repo: str) -> tuple[str, ...]:
        found = await self._http.get_all(PLATFORM, f"/api/v1/repos/{owner}/{repo}/tags")
        return tuple(str(entry["name"]) for entry in found)

    async def _username(self, user_id: int) -> str:
        found = json_of(
            await self._http.call(PLATFORM, "GET", "/api/v1/users/search", params={"uid": user_id})
        )
        people = found.get("data") or []
        if not people:
            raise NotFound(f"no user with id {user_id}")
        return str(people[0]["login"])


def _is_workflow(repo: dict[str, Any]) -> bool:
    return str(repo["name"]).endswith(f".{WORKFLOW}") or WORKFLOW_TOPIC in (
        repo.get("topics") or []
    )


__all__ = ["PRIMITIVE_TOPIC", "WorkflowOps"]
