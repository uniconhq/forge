"""Announcements and clarifications are threads: a titled post with comments,
labelled by kind, held at the forge.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from forge.domain.ids import ContestId, TaskId, ThreadId, WorkspaceId


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
    kind: ThreadKind
    title: str
    body: str
    author_id: int | None
    created_at: datetime
    closed: bool
    answered: bool
    comments: tuple[Comment, ...] = ()
