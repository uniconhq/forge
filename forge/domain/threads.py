"""Announcements and clarifications are threads: a titled post with comments,
labelled by kind, held at the forge. A thread lives at a place, a contest or
a task for an announcement and a contestant's workspace for a clarification,
and is numbered within it. `ThreadChange` is what the forge's event push
says changed: a thread at a place, or the place itself when the event names
no one thread.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.ids import ContestId, TaskId, ThreadId, WorkspaceId
from forge.domain.names import WorkspaceOwner


class ThreadKind(StrEnum):
    ANNOUNCEMENT = "announcement"
    CLARIFICATION = "clarification"


ThreadPlace = ContestId | TaskId | WorkspaceId


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    author_id: int | None
    body: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Thread:
    id: ThreadId
    place: ThreadPlace
    number: int
    kind: ThreadKind
    title: str
    body: str
    author_id: int | None
    created_at: datetime
    closed: bool
    answered: bool
    comments: tuple[Comment, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadChange:
    """A thread that the forge says changed, with what its place is: an
    announcement of a contest, or of one of its tasks (`task`), or a
    clarification on the workspace `asker` owns in the contest.
    """

    thread: ThreadId
    kind: ThreadKind
    contest: ContestId
    task: TaskId | None = None
    asker: WorkspaceOwner | None = None
