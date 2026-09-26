"""Workspaces, submissions and publications at Forgejo. A workspace is a desk
repository plus one submission repository per published task, with the
contestant as a write collaborator. A submission and a publication are
protected versions: tags under a reserved prefix that only the platform
account may create.
"""

import base64
from collections.abc import Sequence

from forge.domain.content import Files
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import ContestId, PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.forges.forgejo.ci import CiOps
from forge.forges.forgejo.content import ContentOps
from forge.forges.forgejo.names import (
    DEFAULT_BRANCH,
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    WorkspaceRef,
    parse_contest,
    parse_task,
    parse_workspace,
    publication_id,
    submission_id,
)
from forge.forges.forgejo.users import IdentityOps

COLLABORATOR_PERMISSION = "write"
PUBLISH_MESSAGE = "Publish"
SUBMIT_MESSAGE = "Submit"


class WorkspaceOps(ContentOps, IdentityOps, CiOps):
    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        parent = parse_contest(contest)
        ref = WorkspaceRef(parent.org, parent.contest, owner)
        repos = [ref.desk_repo] + [ref.submission_repo(parse_task(task).task) for task in tasks]
        usernames = [await self.username_of(member) for member in member_ids]
        for repo in repos:
            await self.create_repo(ref.org, repo, {}, private=True)
            if repo != ref.desk_repo:
                await self.protect_versions(ref.org, repo, SUBMISSION_PREFIX)
            for username in usernames:
                await self._grant_write(ref.org, repo, username)
        return ref.id

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        ref = parse_workspace(workspace)
        usernames = [await self.username_of(member) for member in member_ids]
        repos = await self._workspace_repos(ref)
        for repo in repos:
            for username in usernames:
                await self._http.call(
                    PLATFORM, "DELETE", f"/api/v1/repos/{ref.org}/{repo}/collaborators/{username}"
                )

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = ref.submission_repo(task_name)
        names = await self.version_names(PLATFORM, ref.org, repo, SUBMISSION_PREFIX)
        return tuple(
            submission_id(ref, task_name, int(name.removeprefix(SUBMISSION_PREFIX)))
            for name in sorted(names, key=_number)
        )

    async def record_submission(
        self, workspace: WorkspaceId, task: TaskId, files: Files, *, submitter_id: int
    ) -> SubmissionId:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = ref.submission_repo(task_name)
        await self._write_files(PLATFORM, ref.org, repo, files, message=SUBMIT_MESSAGE)
        head = await self.head_version(PLATFORM, ref.org, repo)
        number = len(await self.version_names(PLATFORM, ref.org, repo, SUBMISSION_PREFIX)) + 1
        await self.create_version(ref.org, repo, f"{SUBMISSION_PREFIX}{number}", head)
        return submission_id(ref, task_name, number)

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        ref = parse_task(task)
        await self._write_files(PLATFORM, ref.org, ref.repo, files, message=PUBLISH_MESSAGE)
        head = await self.head_version(PLATFORM, ref.org, ref.repo)
        number = len(await self.version_names(PLATFORM, ref.org, ref.repo, PUBLISHED_PREFIX)) + 1
        await self.create_version(ref.org, ref.repo, f"{PUBLISHED_PREFIX}{number}", head)
        await self.register_for_grading(task)
        return publication_id(ref, number)

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]:
        ref = parse_task(task)
        names = await self.version_names(PLATFORM, ref.org, ref.repo, PUBLISHED_PREFIX)
        return tuple(
            publication_id(ref, int(name.removeprefix(PUBLISHED_PREFIX)))
            for name in sorted(names, key=_number)
        )

    async def _grant_write(self, org: str, repo: str, username: str) -> None:
        await self._http.call(
            PLATFORM,
            "PUT",
            f"/api/v1/repos/{org}/{repo}/collaborators/{username}",
            json={"permission": COLLABORATOR_PERMISSION},
        )

    async def _workspace_repos(self, ref: WorkspaceRef) -> list[str]:
        owned = await self._http.get_all(PLATFORM, f"/api/v1/orgs/{ref.org}/repos")
        segment = ref.owner.segment
        return [
            str(repo["name"])
            for repo in owned
            if str(repo["name"]) == ref.desk_repo
            or (
                str(repo["name"]).startswith(f"{ref.contest}.")
                and str(repo["name"]).endswith(f".{segment}.sub")
            )
        ]

    async def _write_files(
        self, as_: Identity, org: str, repo: str, files: Files, *, message: str
    ) -> None:
        if not files:
            return
        existing = await self._existing_paths(as_, org, repo)
        first_commit = {"new_branch": DEFAULT_BRANCH} if existing is None else {}
        existing = existing or {}
        await self._http.call(
            as_,
            "POST",
            f"/api/v1/repos/{org}/{repo}/contents",
            json={
                "branch": DEFAULT_BRANCH,
                **first_commit,
                "message": message,
                "files": [
                    {
                        "operation": "update" if path in existing else "create",
                        "path": path,
                        "content": base64.b64encode(content).decode(),
                        **({"sha": existing[path]} if path in existing else {}),
                    }
                    for path, content in sorted(files.items())
                ],
            },
        )

    async def _existing_paths(self, as_: Identity, org: str, repo: str) -> dict[str, str] | None:
        """The blob of every file on the default branch, or none for a
        repository with no commit yet.
        """
        try:
            await self._http.call(
                as_, "GET", f"/api/v1/repos/{org}/{repo}/branches/{DEFAULT_BRANCH}"
            )
        except NotFound:
            return None
        tree = await self._http.call(
            as_,
            "GET",
            f"/api/v1/repos/{org}/{repo}/git/trees/{DEFAULT_BRANCH}",
            params={"recursive": "true", "per_page": 1000},
        )
        entries = tree.json().get("tree") or []
        return {
            str(entry["path"]): str(entry["sha"])
            for entry in entries
            if entry.get("type") == "blob"
        }


def _number(name: str) -> int:
    return int(name.rsplit("/", 1)[-1])
