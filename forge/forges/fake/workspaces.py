"""The workspace area in memory. A publication names a version already
written and keeps its note beside it, as a tag with a message does.
"""

from collections.abc import Sequence
from datetime import datetime

from forge.domain.content import Files
from forge.domain.errors import Conflict, NotFound, Unavailable
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
from forge.forges.fake.state import Repo, State
from forge.forges.ids import (
    PUBLISHED_PREFIX,
    SUBMISSION_PREFIX,
    WorkspaceRef,
    location,
    parse_contest,
    parse_submission,
    parse_task,
    parse_workspace,
    publication_id,
    submission_id,
)

SUBMIT_MESSAGE = "Submit"
NUMBERING_ATTEMPTS = 3


class FakeWorkspaces:
    def __init__(self, state: State) -> None:
        self._state = state

    async def open_workspace(
        self, contest: ContestId, owner: WorkspaceOwner, member_ids: Sequence[int]
    ) -> WorkspaceId:
        self._state.record(
            "open_workspace", PLATFORM, contest=contest, owner=owner, member_ids=list(member_ids)
        )
        self._state.check_up()
        ref = _ref(contest, owner)
        self._open(ref, ref.desk_repo, member_ids)
        return ref.id

    def workspace_of(self, contest: ContestId, owner: WorkspaceOwner) -> WorkspaceId:
        return _ref(contest, owner).id

    async def open_submission_place(
        self, workspace: WorkspaceId, task: TaskId, member_ids: Sequence[int]
    ) -> None:
        self._state.record(
            "open_submission_place",
            PLATFORM,
            workspace=workspace,
            task=task,
            member_ids=list(member_ids),
        )
        self._state.check_up()
        ref = parse_workspace(workspace)
        task_ref = parse_task(task)
        if (task_ref.org, task_ref.contest) != (ref.org, ref.contest):
            raise NotFound(f"{task} is not a task of the contest {workspace} is in")
        self._open(ref, ref.submission_repo(task_ref.task), member_ids, reserve=True)

    def _open(
        self, ref: WorkspaceRef, name: str, member_ids: Sequence[int], *, reserve: bool = False
    ) -> None:
        """One repository of the workspace, its submissions reserved before
        anyone may write it, as the Forgejo implementation does.
        """
        scope = Scope(ref.org, ref.contest)
        repo = self._state.repos.get((ref.org, name))
        if repo is None:
            repo = self._state.create_repo(PLATFORM, ref.org, name, {}, scope=scope)
        repo.rewrites_refused = True
        repo.teams.add(scope)
        if reserve:
            repo.reserved.add(SUBMISSION_PREFIX)
        repo.writers.update(member_ids)

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        self._state.record(
            "close_workspace", PLATFORM, workspace=workspace, member_ids=list(member_ids)
        )
        for repo in self._workspace_repos(parse_workspace(workspace)):
            repo.writers.difference_update(member_ids)

    async def list_submissions(self, workspace: WorkspaceId, task: TaskId) -> tuple[Submitted, ...]:
        self._state.record("list_submissions", PLATFORM, workspace=workspace, task=task)
        self._state.check_up()
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = self._state.repo(ref.org, ref.submission_repo(task_name))
        return tuple(
            _submitted(repo, ref, task_name, number, self._state.clock.now())
            for number in _numbers(repo, SUBMISSION_PREFIX)
        )

    async def record_submission(
        self, as_: Identity, workspace: WorkspaceId, task: TaskId, files: Files, *, key: str
    ) -> Submitted:
        """Commit the files as `as_` over whatever the place held, then name
        the change as the next submission as the platform, the way the Forgejo
        implementation does. `racing_submissions` makes that many other
        submissions take the number first, and `lose_submission_answer`
        names the submission and then fails as if the answer were lost.
        """
        self._state.record("record_submission", as_, workspace=workspace, task=task, key=key)
        self._state.check_up()
        ref = parse_workspace(workspace)
        task_name = parse_task(task).task
        repo = self._state.repo(ref.org, ref.submission_repo(task_name))
        self._state.require_write(as_, repo)
        written: dict[str, bytes | None] = {path: None for path in repo.files if path not in files}
        written.update(files)
        version = self._state.commit(repo, written, SUBMIT_MESSAGE, self._state.author(as_))
        for _ in range(NUMBERING_ATTEMPTS):
            numbers = _numbers(repo, SUBMISSION_PREFIX)
            number = (numbers[-1] if numbers else 0) + 1
            name = f"{SUBMISSION_PREFIX}{number}"
            if self._state.racing_submissions:
                self._state.racing_submissions -= 1
                self._state.create_version(PLATFORM, repo, name, at=version)
            try:
                self._state.create_version(PLATFORM, repo, name, at=version, note=write_note(key))
            except Conflict:
                continue
            if self._state.lose_submission_answer:
                self._state.lose_submission_answer = False
                raise Unavailable("the answer to naming the submission was lost")
            return _submitted(repo, ref, task_name, number, self._state.clock.now())
        raise Conflict(f"could not number the next submission in {ref.submission_repo(task_name)}")

    async def read_submission_file(
        self, as_: Identity, submission: SubmissionId, path: str
    ) -> bytes:
        self._state.record("read_submission_file", as_, submission=submission, path=path)
        self._state.check_up()
        ref, task_name, number = parse_submission(submission)
        repo = self._state.repo(ref.org, ref.submission_repo(task_name))
        self._state.require_read(as_, repo)
        files = self._state.version_files(repo, f"{SUBMISSION_PREFIX}{number}")
        if path not in files:
            raise NotFound(f"{submission} has no file {path}")
        return files[path]

    async def publish(self, task: TaskId, at: VersionId, note: str) -> PublicationId:
        self._state.record("publish", PLATFORM, task=task, at=at)
        self._state.check_up()
        ref = parse_task(task)
        repo = self._state.repo(*location(task))
        if at not in repo.snapshots:
            raise NotFound(f"{task} has no version {at}")
        numbers = _numbers(repo, PUBLISHED_PREFIX)
        number = (numbers[-1] if numbers else 0) + 1
        self._state.create_version(PLATFORM, repo, f"{PUBLISHED_PREFIX}{number}", at=at, note=note)
        self._state.published_at[(task, number)] = self._state.clock.now()
        return publication_id(ref, number)

    async def list_publications(self, task: TaskId) -> tuple[Publication, ...]:
        self._state.record("list_publications", PLATFORM, task=task)
        self._state.check_up()
        ref = parse_task(task)
        repo = self._state.repo(*location(task))
        publications = []
        for number in _numbers(repo, PUBLISHED_PREFIX):
            name = f"{PUBLISHED_PREFIX}{number}"
            note = read_note(repo.notes.get(name))
            publications.append(
                Publication(
                    id=publication_id(ref, number),
                    number=number,
                    version=VersionId(repo.versions[name]),
                    grading_changed=note.grading_changed,
                    changes=note.changes,
                    at=self._state.published_at.get((task, number), self._state.clock.now()),
                )
            )
        return tuple(publications)

    def _workspace_repos(self, ref: WorkspaceRef) -> list[Repo]:
        return [
            repo
            for (owner, name), repo in self._state.repos.items()
            if owner == ref.org and (name == ref.desk_repo or ref.is_submission_repo(name))
        ]


def _ref(contest: ContestId, owner: WorkspaceOwner) -> WorkspaceRef:
    parent = parse_contest(contest)
    return WorkspaceRef(parent.org, parent.contest, owner)


def _submitted(repo: Repo, ref: WorkspaceRef, task: str, number: int, now: datetime) -> Submitted:
    name = f"{SUBMISSION_PREFIX}{number}"
    return Submitted(
        id=submission_id(ref, task, number),
        number=number,
        version=VersionId(repo.versions[name]),
        key=read_submission_note(repo.notes.get(name)),
        at=repo.version_times.get(name, now),
    )


def _numbers(repo: Repo, prefix: str) -> list[int]:
    return sorted(
        int(name.removeprefix(prefix)) for name in repo.versions if name.startswith(prefix)
    )
