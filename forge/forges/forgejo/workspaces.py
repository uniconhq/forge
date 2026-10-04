"""The workspace area over Forgejo. A workspace is a desk repository plus one
submission repository per task, each attached to the contest's role teams so
its organisers can read it, with the members as write collaborators. Each is
made on its own, and every part of making one checks before it acts, so a
try that stopped halfway is finished by the next. A submission and a
publication are tags under a reserved prefix that only the platform account
may create, each pointing at the exact commit the files went in with. A
publication is an annotated tag whose message is its note.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from forge.domain.content import Files
from forge.domain.errors import Conflict, NotFound
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
from forge.domain.submissions import Submitted, write_note
from forge.domain.submissions import read_note as read_submission_note
from forge.forges.forgejo.repos import Repos
from forge.forges.forgejo.teams import Teams
from forge.forges.forgejo.users import Users
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    WorkspaceRef,
    parse_contest,
    parse_submission,
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
        self, contest: ContestId, owner: WorkspaceOwner, member_ids: Sequence[int]
    ) -> WorkspaceId:
        ref = _ref(contest, owner)
        await self._open(ref, ref.desk_repo, member_ids)
        return ref.id

    def workspace_of(self, contest: ContestId, owner: WorkspaceOwner) -> WorkspaceId:
        return _ref(contest, owner).id

    async def open_submission_place(
        self, workspace: WorkspaceId, task: TaskId, member_ids: Sequence[int]
    ) -> None:
        ref = parse_workspace(workspace)
        task_ref = parse_task(task)
        if (task_ref.org, task_ref.contest) != (ref.org, ref.contest):
            raise NotFound(f"{task} is not a task of the contest {workspace} is in")
        await self._open(ref, ref.submission_repo(task_ref.task), member_ids, reserve=True)

    async def _open(
        self, ref: WorkspaceRef, repo: str, member_ids: Sequence[int], *, reserve: bool = False
    ) -> None:
        """Make one repository of the workspace, empty and private, unless it
        is there: only the platform makes repositories, so one of that name
        is an earlier try's. Then refuse rewriting its history, attach the
        contest's roles, with `reserve` keep its submissions for the platform,
        and only then give the members write access, so nobody can write a
        place whose submissions anyone may name. Each leaves what is already
        right alone.

        A repository that is there and has a collaborator who is not one of
        the members is somebody else's, and is refused with `Conflict` rather
        than shared: the name is the owner's id, so this only happens if
        something other than this code made or changed it.
        """
        usernames = [await self._users.username_of(member) for member in member_ids]
        if await self._repos.exists(ref.org, repo):
            await self._refuse_if_someone_elses(ref.org, repo, usernames)
        else:
            try:
                await self._repos.create(PLATFORM, ref.org, repo, {}, private=True)
            except Conflict:
                if not await self._repos.exists(ref.org, repo):
                    raise
                await self._refuse_if_someone_elses(ref.org, repo, usernames)
        await self._repos.protect_branch(ref.org, repo)
        await self._teams.attach_scope(Scope(ref.org, ref.contest), repo)
        if reserve:
            await self._repos.reserve_versions(ref.org, repo, SUBMISSION_PREFIX)
        for username in usernames:
            await self._repos.add_collaborator(ref.org, repo, username, permission=WRITE)

    async def _refuse_if_someone_elses(self, org: str, repo: str, usernames: list[str]) -> None:
        members = {username.lower() for username in usernames}
        others = sorted(
            str(person["login"])
            for person in await self._repos.collaborators(org, repo)
            if str(person["login"]).lower() not in members
        )
        if others:
            raise Conflict(f"{org}/{repo} already has other collaborators: {', '.join(others)}")

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        ref = parse_workspace(workspace)
        usernames = [await self._users.username_of(member) for member in member_ids]
        for repo in await self._workspace_repos(ref):
            for username in usernames:
                await self._repos.remove_collaborator(ref.org, repo, username)

    async def list_submissions(self, workspace: WorkspaceId, task: TaskId) -> tuple[Submitted, ...]:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        tags = await self._repos.tags(PLATFORM, ref.org, ref.submission_repo(task_name))
        found = [
            _submitted(ref, task_name, tag)
            for tag in tags
            if str(tag["name"]).startswith(SUBMISSION_PREFIX)
            and str(tag["name"]).removeprefix(SUBMISSION_PREFIX).isdigit()
        ]
        return tuple(sorted(found, key=lambda submitted: submitted.number))

    async def record_submission(
        self, as_: Identity, workspace: WorkspaceId, task: TaskId, files: Files, *, key: str
    ) -> Submitted:
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = ref.submission_repo(task_name)
        written = await self._repos.replace_files(as_, ref.org, repo, files, message=SUBMIT_MESSAGE)
        number = await self._next_version(
            ref.org, repo, SUBMISSION_PREFIX, written, note=write_note(key)
        )
        for tag in await self._repos.tags(PLATFORM, ref.org, repo):
            if tag.get("name") == f"{SUBMISSION_PREFIX}{number}":
                return _submitted(ref, task_name, tag)
        raise NotFound(f"the submission {number} is not listed after it was made")

    async def read_submission_file(
        self, as_: Identity, submission: SubmissionId, path: str, *, max_size: int
    ) -> bytes:
        ref, task_name, number = parse_submission(submission)
        return await self._repos.read_raw(
            as_,
            ref.org,
            ref.submission_repo(task_name),
            path,
            at=f"{SUBMISSION_PREFIX}{number}",
            max_size=max_size,
        )

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
                    workflows=note.workflows,
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


def _submitted(ref: WorkspaceRef, task: str, tag: dict[str, Any]) -> Submitted:
    number = int(str(tag["name"]).removeprefix(SUBMISSION_PREFIX))
    commit = tag.get("commit") or {}
    return Submitted(
        id=submission_id(ref, task, number),
        number=number,
        version=VersionId(str(commit["sha"])),
        key=read_submission_note(tag.get("message")),
        at=datetime.fromisoformat(str(commit["created"])),
    )


def _ref(contest: ContestId, owner: WorkspaceOwner) -> WorkspaceRef:
    parent = parse_contest(contest)
    return WorkspaceRef(parent.org, parent.contest, owner)
