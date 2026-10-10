"""How the platform's objects are named at the host, and how the opaque ids
the port hands out are built and read. Both implementations use this one
grammar, so an id is read the same way, and a malformed one is refused the
same way, whichever is behind the port. Nothing above the port sees these
shapes.

Every repository the platform creates joins its segments with dots and ends
in a fixed word that says what it is:

    <org>/<contest>.contest
    <org>/<contest>.<task>.task
    <org>/<contest>.<owner>.desk
    <org>/<contest>.<task>.<owner>.sub
    <owner>/<name>.workflow
    unicon/<name>.primitive

The owner of a workspace is `u<user-id>` for a contestant, or
`team.<team-id>` for a team, and is read
back as everything between the second dot and the final word. Never a
username: a person can change theirs and someone else can then take it, and
the workspace would follow the name.
A workspace is per contest, so its desk carries the contest too: the same
person in two contests of one org has two desks, and closing one contest's
workspace leaves the other's alone.
"""

import json
import uuid
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
from forge.domain.names import TEAM_PREFIX, USER_PREFIX, TeamOwner, UserOwner, WorkspaceOwner
from forge.domain.threads import ThreadChange, ThreadKind

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
        return f"{self.contest}.{self.owner.segment}.{DESK}"

    def submission_repo(self, task: str) -> str:
        return f"{self.contest}.{task}.{self.owner.segment}.{SUBMISSION}"

    def is_submission_repo(self, name: str) -> bool:
        return name.startswith(f"{self.contest}.") and name.endswith(
            f".{self.owner.segment}.{SUBMISSION}"
        )

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


def task_of_repo(owner: str, name: str) -> TaskRef:
    """The task a repository is, by its owner and name. `MalformedId` for a
    repository that is no task's.
    """
    stem, dot, word = name.rpartition(".")
    contest, _, task = stem.partition(".")
    if not dot or word != TASK or not contest or not task or "." in task or not owner:
        raise MalformedId(f"{owner}/{name} is not a task")
    return TaskRef(owner, contest, task)


def parse_workspace(value: WorkspaceId) -> WorkspaceRef:
    org, contest, owner = _split(value, 3)
    if not owner.startswith(WORKSPACE_MARK):
        raise MalformedId(f"{value} is not a workspace")
    return WorkspaceRef(org, contest, owner_from_segment(owner.removeprefix(WORKSPACE_MARK)))


def owner_from_segment(segment: str) -> WorkspaceOwner:
    """The owner a workspace segment names: a team by its id after the team
    prefix, and a contestant by
    their user id after the user prefix. Anything else names no workspace.
    """
    if segment.startswith(TEAM_PREFIX):
        try:
            return TeamOwner(uuid.UUID(segment.removeprefix(TEAM_PREFIX)))
        except ValueError:
            raise MalformedId(f"{segment} is not a team") from None
    user = segment.removeprefix(USER_PREFIX)
    if segment.startswith(USER_PREFIX) and user.isdigit():
        return UserOwner(int(user))
    raise MalformedId(f"{segment} is not a workspace owner")


def is_workspace(value: str) -> bool:
    parts = value.split("/")
    return len(parts) == 3 and parts[2].startswith(WORKSPACE_MARK)


def parse_workflow(value: WorkflowId) -> WorkflowRef:
    owner, name = _split(value, 2)
    return WorkflowRef(owner, name)


def location(place: str) -> tuple[str, str]:
    """The owner and repository a contest, task or workspace id names: a
    workspace's is its desk.
    """
    if is_workspace(place):
        workspace = parse_workspace(WorkspaceId(place))
        return workspace.org, workspace.desk_repo
    if place.count("/") == 1:
        contest = parse_contest(ContestId(place))
        return contest.org, contest.repo
    task = parse_task(TaskId(place))
    return task.org, task.repo


def place_of_repo(owner: str, name: str) -> ContestId | TaskId | WorkspaceId:
    """The contest, task or workspace whose repository this is, the inverse
    of `location`: a contest's or a task's own repository, or a workspace's
    desk. `MalformedId` for any other repository.
    """
    stem, dot, word = name.rpartition(".")
    if not dot or not owner or not stem:
        raise MalformedId(f"{owner}/{name} holds no threads")
    if word == CONTEST and "." not in stem:
        return ContestRef(owner, stem).id
    if word == TASK:
        return task_of_repo(owner, name).id
    contest, _, segment = stem.partition(".")
    if word == DESK and contest and segment:
        return WorkspaceRef(owner, contest, owner_from_segment(segment)).id
    raise MalformedId(f"{owner}/{name} holds no threads")


def thread_change(owner: str, name: str, number: int) -> ThreadChange:
    """The change to the thread numbered `number` in the repository: an
    announcement for a contest's or a task's own, a clarification for a
    desk. `MalformedId` for any other repository.
    """
    place = place_of_repo(owner, name)
    made = thread_id(owner, name, number)
    if is_workspace(place):
        workspace = parse_workspace(WorkspaceId(place))
        contest = ContestRef(workspace.org, workspace.contest).id
        return ThreadChange(made, ThreadKind.CLARIFICATION, contest, asker=workspace.owner)
    if place.count("/") == 1:
        return ThreadChange(made, ThreadKind.ANNOUNCEMENT, ContestId(place))
    task = parse_task(TaskId(place))
    return ThreadChange(
        made, ThreadKind.ANNOUNCEMENT, ContestRef(task.org, task.contest).id, task=task.id
    )


THREAD_EVENTS = frozenset({"issues", "issue_comment"})


def thread_pushed(kind: str, body: bytes) -> ThreadChange | None:
    """The thread a forge push of `kind` names, read from its body, or none
    for a push that names no thread of the platform's: another kind of push,
    a pull request, a repository the platform did not name, or an issue that
    does not carry the label of the thread its repository holds, such as an
    organiser's own issue on a contest's repository.
    """
    if kind not in THREAD_EVENTS:
        return None
    try:
        event = json.loads(body)
        repository = event["repository"]
        issue = event["issue"]
        if issue.get("pull_request"):
            return None
        owner = str((repository.get("owner") or {}).get("login") or "")
        change = thread_change(owner, str(repository["name"]), int(issue["number"]))
        labels = {str(label["name"]) for label in issue.get("labels") or []}
    except ValueError, KeyError, TypeError, AttributeError, MalformedId:
        return None
    return change if change.kind.value in labels else None


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
