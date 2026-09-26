"""The workspace area in memory."""

from collections.abc import Sequence

from forge.domain.content import Files
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, PublicationId, SubmissionId, TaskId, WorkspaceId
from forge.domain.names import WorkspaceOwner
from forge.domain.roles import Scope
from forge.forges.fake import ids
from forge.forges.fake.state import PUBLISHED_PREFIX, SUBMISSION_PREFIX, Repo, State
from forge.port.grading import GradingPort


class FakeWorkspaces:
    def __init__(self, state: State, grading: GradingPort) -> None:
        self._state = state
        self._grading = grading

    async def open_workspace(
        self,
        contest: ContestId,
        owner: WorkspaceOwner,
        member_ids: Sequence[int],
        tasks: Sequence[TaskId],
    ) -> WorkspaceId:
        self._state.record(
            "open_workspace", PLATFORM, contest=contest, owner=owner, member_ids=list(member_ids)
        )
        org, contest_name = ids.contest_parts(contest)
        scope = Scope(org, contest_name)
        desk = self._state.create_repo(org, ids.desk_repo(owner.segment), {}, scope=scope)
        desk.writers.update(member_ids)
        for task in tasks:
            task_name = ids.task_parts(task)[2]
            sub = self._state.create_repo(
                org, ids.submission_repo(contest_name, task_name, owner.segment), {}, scope=scope
            )
            sub.writers.update(member_ids)
        return ids.workspace(contest, owner.segment)

    async def close_workspace(self, workspace: WorkspaceId, member_ids: Sequence[int]) -> None:
        self._state.record(
            "close_workspace", PLATFORM, workspace=workspace, member_ids=list(member_ids)
        )
        for repo in self._workspace_repos(workspace):
            repo.writers.difference_update(member_ids)

    async def list_submissions(
        self, workspace: WorkspaceId, task: TaskId
    ) -> tuple[SubmissionId, ...]:
        self._state.record("list_submissions", PLATFORM, workspace=workspace, task=task)
        repo = self._submission_repo(workspace, task)
        task_name = ids.task_parts(task)[2]
        return tuple(
            ids.submission(workspace, task_name, number)
            for number in _numbers(repo, SUBMISSION_PREFIX)
        )

    async def record_submission(
        self, workspace: WorkspaceId, task: TaskId, files: Files, *, submitter_id: int
    ) -> SubmissionId:
        self._state.record(
            "record_submission",
            PLATFORM,
            workspace=workspace,
            task=task,
            submitter_id=submitter_id,
        )
        repo = self._submission_repo(workspace, task)
        self._state.commit(repo, files, "Submit", submitter_id)
        number = self._state.next_number(repo, SUBMISSION_PREFIX)
        self._state.create_version(PLATFORM, repo, f"{SUBMISSION_PREFIX}{number}")
        return ids.submission(workspace, ids.task_parts(task)[2], number)

    async def publish(self, task: TaskId, files: Files) -> PublicationId:
        self._state.record("publish", PLATFORM, task=task)
        repo = task_repo(self._state, task)
        self._state.commit(repo, files, "Publish", None)
        number = self._state.next_number(repo, PUBLISHED_PREFIX)
        self._state.create_version(PLATFORM, repo, f"{PUBLISHED_PREFIX}{number}")
        await self._grading.register(task)
        return PublicationId(f"{task}#{number}")

    async def list_publications(self, task: TaskId) -> tuple[PublicationId, ...]:
        self._state.record("list_publications", PLATFORM, task=task)
        repo = task_repo(self._state, task)
        return tuple(
            PublicationId(f"{task}#{number}") for number in _numbers(repo, PUBLISHED_PREFIX)
        )

    def _submission_repo(self, workspace: WorkspaceId, task: TaskId) -> Repo:
        org, contest_name, segment = ids.workspace_parts(workspace)
        task_name = ids.task_parts(task)[2]
        return self._state.repo(org, ids.submission_repo(contest_name, task_name, segment))

    def _workspace_repos(self, workspace: WorkspaceId) -> list[Repo]:
        org, contest_name, segment = ids.workspace_parts(workspace)
        return [
            repo
            for (owner, name), repo in self._state.repos.items()
            if owner == org
            and (
                name == ids.desk_repo(segment)
                or (name.startswith(f"{contest_name}.") and name.endswith(f".{segment}.sub"))
            )
        ]


def task_repo(state: State, task: TaskId) -> Repo:
    org, contest_name, name = ids.task_parts(task)
    return state.repo(org, ids.task_repo(contest_name, name))


def _numbers(repo: Repo, prefix: str) -> list[int]:
    return sorted(
        int(name.removeprefix(prefix)) for name in repo.versions if name.startswith(prefix)
    )
