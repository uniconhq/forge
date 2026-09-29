"""The workspace area over Forgejo. A workspace is a desk repository plus one
submission repository per task, attached to the contest's role teams so its
organisers can read them, with the members as write collaborators. A
submission and a publication are tags under a reserved prefix that only the
platform account may create, each pointing at the exact commit the files
went in with. A publication is an annotated tag whose message is its note.
"""

from collections.abc import Sequence
from datetime import datetime

from forge.domain.content import Files
from forge.domain.errors import Conflict
from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import (
    ContestId,
    PublicationId,
    SubmissionId,
    TaskId,
    VersionId,
    WorkspaceId,
)
from forge.domain.names import WorkspaceOwner
from forge.domain.publications import Publication, read_note
from forge.domain.roles import Scope
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.forgejo.users import Users
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    WorkspaceRef,
    parse_contest,
    parse_task,
    parse_workspace,
    publication_id,
    submission_id,
)

WRITE = "write"
SUBMIT_MESSAGE = "Submit"
NUMBERING_ATTEMPTS = 3


class ForgejoWorkspaces:
    def __init__(self, repos: Repos, users: Users, teams: Teams) -> None:
        self._repos = repos
        self._users = users
        self._teams = teams

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
            await self._repos.protect_branch(ref.org, repo)
            await self._teams.attach_scope(Scope(ref.org, ref.contest), repo)
            if repo != ref.desk_repo:
                await self._repos.reserve_versions(ref.org, repo, SUBMISSION_PREFIX)
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
        self, as_: Identity, workspace: WorkspaceId, task: TaskId, files: Files
    ) -> SubmissionId:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = ref.submission_repo(task_name)
        written = await self._repos.write_files(as_, ref.org, repo, files, message=SUBMIT_MESSAGE)
        number = await self._next_version(ref.org, repo, SUBMISSION_PREFIX, written)
        return submission_id(ref, task_name, number)

    async def publish(self, task: TaskId, at: VersionId, note: str) -> PublicationId:
        ref = parse_task(task)
        number = await self._next_version(ref.org, ref.repo, PUBLISHED_PREFIX, at, note=note)
        return publication_id(ref, number)

    async def list_publications(self, task: TaskId) -> tuple[Publication, ...]:
        ref = parse_task(task)
        publications = []
        for tag in await self._repos.tags(PLATFORM, ref.org, ref.repo):
            name = str(tag["name"])
            number = name.removeprefix(PUBLISHED_PREFIX)
            if not name.startswith(PUBLISHED_PREFIX) or not number.isdigit():
                continue
            note = read_note(tag.get("message"))
            commit = tag.get("commit") or {}
            publications.append(
                Publication(
                    id=publication_id(ref, int(number)),
                    number=int(number),
                    version=VersionId(str(commit["sha"])),
                    grading_changed=note.grading_changed,
                    changes=note.changes,
                    at=datetime.fromisoformat(str(commit["created"])),
                )
            )
        return tuple(sorted(publications, key=lambda publication: publication.number))

    async def _next_version(
        self, org: str, repo: str, prefix: str, target: str | None, *, note: str | None = None
    ) -> int:
        """Create the next protected version at `target`, the commit the
        caller just made, numbered after the highest that exists, carrying
        `note` when one is given. With no commit to name, the head is what
        there is. Two callers racing for one number collide at the host, and
        the loser takes the next.
        """
        at = target or await self._repos.head(PLATFORM, org, repo)
        for _ in range(NUMBERING_ATTEMPTS):
            numbers = await self._numbers(org, repo, prefix)
            number = (numbers[-1] if numbers else 0) + 1
            try:
                await self._repos.create_version(
                    PLATFORM, org, repo, f"{prefix}{number}", at, message=note
                )
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
        return [
            str(repo["name"])
            for repo in await self._repos.under(ref.org)
            if str(repo["name"]) == ref.desk_repo or ref.is_submission_repo(str(repo["name"]))
        ]
