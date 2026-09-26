"""How the platform's objects are named at Forgejo, and how the opaque ids the
port hands out are built and read. Nothing above the port sees these shapes.

Every repository the platform creates joins its segments with dots and ends
in a fixed word that says what it is:

    <org>/<contest>.contest
    <org>/<contest>.<task>.task
    <org>/<owner>.desk
    <org>/<contest>.<task>.<owner>.sub
    <owner>/<name>.workflow
    unicon/<name>.primitive

The owner of a workspace is the contestant's username, lower cased, or
`team.<team-id>`, and is read back as everything between the second dot and
the final word.
"""

from dataclasses import dataclass

from forge.domain.errors import NotFound
from forge.domain.ids import (
    ContestId,
    PrimitiveId,
    PublicationId,
    SubmissionId,
    TaskId,
    ThreadId,
    WorkflowId,
    WorkspaceId,
)
from forge.domain.names import WorkspaceOwner, owner_from_segment

PLATFORM_ACCOUNT = "unicon-backend"
CI_ADMIN_ACCOUNT = "unicon-ci"
PLATFORM_ORG = "unicon"
WORKSPACE_MARK = "@"

CONTEST = "contest"
TASK = "task"
DESK = "desk"
SUBMISSION = "sub"
WORKFLOW = "workflow"
PRIMITIVE = "primitive"

PUBLISHED_PREFIX = "published/"
SUBMISSION_PREFIX = "submission/"
PROTECTED_PREFIXES = (PUBLISHED_PREFIX, SUBMISSION_PREFIX)
DEFAULT_BRANCH = "main"

WORKFLOW_TOPIC = "unicon-workflow"
PRIMITIVE_TOPIC = "unicon-primitive"


class MalformedId(NotFound):
    """An id that did not come from this implementation names nothing."""


@dataclass(frozen=True, slots=True)
class ContestRef:
    org: str
    contest: str

    @property
    def repo(self) -> str:
        return f"{self.contest}.{CONTEST}"

    @property
    def id(self) -> ContestId:
        return ContestId(f"{self.org}/{self.contest}")


@dataclass(frozen=True, slots=True)
class TaskRef:
    org: str
    contest: str
    task: str

    @property
    def repo(self) -> str:
        return f"{self.contest}.{self.task}.{TASK}"

    @property
    def id(self) -> TaskId:
        return TaskId(f"{self.org}/{self.contest}/{self.task}")


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    org: str
    contest: str
    owner: WorkspaceOwner

    @property
    def desk_repo(self) -> str:
        return f"{self.owner.segment}.{DESK}"

    def submission_repo(self, task: str) -> str:
        return f"{self.contest}.{task}.{self.owner.segment}.{SUBMISSION}"

    @property
    def id(self) -> WorkspaceId:
        return WorkspaceId(f"{self.org}/{self.contest}/{WORKSPACE_MARK}{self.owner.segment}")


@dataclass(frozen=True, slots=True)
class WorkflowRef:
    owner: str
    name: str

    @property
    def repo(self) -> str:
        return f"{self.name}.{WORKFLOW}"

    @property
    def id(self) -> WorkflowId:
        return WorkflowId(f"{self.owner}/{self.name}")


def parse_contest(value: ContestId) -> ContestRef:
    org, contest = _split(value, 2)
    return ContestRef(org, contest)


def parse_task(value: TaskId) -> TaskRef:
    org, contest, task = _split(value, 3)
    return TaskRef(org, contest, task)


def parse_workspace(value: WorkspaceId) -> WorkspaceRef:
    org, contest, owner = _split(value, 3)
    if not owner.startswith(WORKSPACE_MARK):
        raise MalformedId(f"{value} is not a workspace")
    return WorkspaceRef(org, contest, owner_from_segment(owner.removeprefix(WORKSPACE_MARK)))


def is_workspace(value: str) -> bool:
    parts = value.split("/")
    return len(parts) == 3 and parts[2].startswith(WORKSPACE_MARK)


def parse_workflow(value: WorkflowId) -> WorkflowRef:
    owner, name = _split(value, 2)
    return WorkflowRef(owner, name)


def primitive_repo(value: PrimitiveId) -> str:
    return f"{value}.{PRIMITIVE}"


def publication_id(task: TaskRef, number: int) -> PublicationId:
    return PublicationId(f"{task.id}#{number}")


def parse_publication(value: PublicationId) -> tuple[TaskRef, int]:
    task, number = _numbered(value)
    return parse_task(TaskId(task)), number


def submission_id(workspace: WorkspaceRef, task: str, number: int) -> SubmissionId:
    return SubmissionId(f"{workspace.id}/{task}#{number}")


def parse_submission(value: SubmissionId) -> tuple[WorkspaceRef, str, int]:
    head, number = _numbered(value)
    org, contest, owner, task = _split(head, 4)
    return parse_workspace(WorkspaceId(f"{org}/{contest}/{owner}")), task, number


def thread_id(owner: str, repo: str, number: int) -> ThreadId:
    return ThreadId(f"{owner}/{repo}#{number}")


def parse_thread(value: ThreadId) -> tuple[str, str, int]:
    head, number = _numbered(value)
    owner, repo = _split(head, 2)
    return owner, repo, number


def _split(value: str, parts: int) -> list[str]:
    pieces = value.split("/")
    if len(pieces) != parts or not all(pieces):
        raise MalformedId(f"{value} is not an id of this forge")
    return pieces


def _numbered(value: str) -> tuple[str, int]:
    head, separator, number = value.rpartition("#")
    if not separator or not number.isdigit():
        raise MalformedId(f"{value} is not an id of this forge")
    return head, int(number)
