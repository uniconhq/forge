"""The workspace area over Forgejo. A workspace is a desk repository plus one
submission repository per task, attached to the contest's role teams so its
organisers can read them, with the members as write collaborators. A
submission and a publication are tags under a reserved prefix that only the
platform account may create.
"""

from collections.abc import Sequence

from forge.domain.content import Files
from forge.domain.errors import Conflict
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.domain.roles import Scope
from forge.forges.forgejo.names import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    WorkspaceRef,
    parse_contest,
    parse_task,
    parse_workspace,
    publication_id,
    submission_id,
)
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.forgejo.users import Users
from forge.port.grading import GradingPort

WRITE = "write"
SUBMIT_MESSAGE = "Submit"
PUBLISH_MESSAGE = "Publish"
NUMBERING_ATTEMPTS = 3


class ForgejoWorkspaces:
    def __init__(self, repos: Repos, users: Users, teams: Teams, grading: GradingPort) -> None:
        self._repos = repos
        self._users = users
        self._teams = teams
        self._grading = grading

    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        parent = parse_contest(contest)
        ref = WorkspaceRef(parent.org, parent.contest, owner)
        usernames = [await self._users.username_of(member) for member in member_ids]
        repos = [ref.desk_repo] + [ref.submission_repo(parse_task(task).task) for task in tasks]
        for repo in repos:
            await self._repos.create(PLATFORM, ref.org, repo, {}, private=True)
            await self._teams.attach_scope(Scope(ref.org, ref.contest), repo)
            if repo != ref.desk_repo:
                await self._repos.protect_versions(ref.org, repo, SUBMISSION_PREFIX)
            for username in usernames:
                await self._repos.add_collaborator(
                    PLATFORM, ref.org, repo, username, permission=WRITE
                )
        return ref.id

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        ref = parse_workspace(workspace)
        usernames = [await self._users.username_of(member) for member in member_ids]
        for repo in await self._workspace_repos(ref):
            for username in usernames:
                await self._repos.remove_collaborator(PLATFORM, ref.org, repo, username)

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        numbers = await self._numbers(ref.org, ref.submission_repo(task_name), SUBMISSION_PREFIX)
        return tuple(submission_id(ref, task_name, number) for number in numbers)

    async def record_submission(
        self, workspace: WorkspaceId, task: TaskId, files: Files, *, submitter_id: int
    ) -> SubmissionId:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = ref.submission_repo(task_name)
        await self._repos.write_files(PLATFORM, ref.org, repo, files, message=SUBMIT_MESSAGE)
        number = await self._next_version(ref.org, repo, SUBMISSION_PREFIX)
        return submission_id(ref, task_name, number)

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        ref = parse_task(task)
        await self._repos.write_files(PLATFORM, ref.org, ref.repo, files, message=PUBLISH_MESSAGE)
        number = await self._next_version(ref.org, ref.repo, PUBLISHED_PREFIX)
        await self._grading.register(task)
        return publication_id(ref, number)

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]:
        ref = parse_task(task)
        numbers = await self._numbers(ref.org, ref.repo, PUBLISHED_PREFIX)
        return tuple(publication_id(ref, number) for number in numbers)

    async def _next_version(self, org: str, repo: str, prefix: str) -> int:
        """Create the next protected version at the head, numbered after the
        highest that exists. Two callers racing for one number collide at the
        host, and the loser takes the next.
        """
        head = await self._repos.head(PLATFORM, org, repo)
        for _ in range(NUMBERING_ATTEMPTS):
            numbers = await self._numbers(org, repo, prefix)
            number = (numbers[-1] if numbers else 0) + 1
            try:
                await self._repos.create_version(PLATFORM, org, repo, f"{prefix}{number}", head)
            except Conflict:
                continue
            return number
        raise Conflict(f"could not number the next version under {prefix} in {org}/{repo}")

    async def _numbers(self, org: str, repo: str, prefix: str) -> list[int]:
        names = await self._repos.versions(PLATFORM, org, repo)
        return sorted(
            int(name.removeprefix(prefix))
            for name in names
            if name.startswith(prefix) and name.removeprefix(prefix).isdigit()
        )

    async def _workspace_repos(self, ref: WorkspaceRef) -> list[str]:
        segment = ref.owner.segment
        return [
            str(repo["name"])
            for repo in await self._repos.under(ref.org)
            if str(repo["name"]) == ref.desk_repo
            or (
                str(repo["name"]).startswith(f"{ref.contest}.")
                and str(repo["name"]).endswith(f".{segment}.sub")
            )
        ]
