"""Whether a row sees a task and may submit to it, and when its submissions
are late, worked out from `contest.yaml` and the clock on every read
(TASK-FORMAT.md section 1.1). Nobody releases a task and nothing fires when
the moment comes. A row is a contestant, or a team when the contest has
teams.

A task's timeline is its entry in the contest's `tasks`. It is released once
its contest has been published and has started and the entry's
`release_at`, the contest's `start` when it gives none, has passed. A task
the contest does not list has no timeline and is never released. An
archived contest was published before, so its tasks stay released. A task
is visible while it is released and its contest is not archived, so
contestants keep the statement and their results after the end. It is open
to a row while it is visible and the row's close has not passed: the
entry's `closes`, the contest's `end` when it gives none, plus the row's
extension on the task. A start or a release time counts from its own
instant; a close is the first instant that is closed.

A submission made after the row's due, the entry's `due` plus the row's
extension on the task, is late by the number of started days of 86,400
seconds since, which the scoring turns into points. A task reveals, showing
what was held back, once it has closed for every row, the longest extension
on it included.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from forge.domain.definitions import ContestDefinition, ContestTask, ContestVisibility, State

DAY = timedelta(days=1)


class Closed(StrEnum):
    """Why a task is not open to a row, the first of these that applies; and,
    to a person who holds no approved row, `not_approved` where it is
    otherwise open, as a submit would be refused.
    """

    NOT_RELEASED = "not_released"
    ARCHIVED = "archived"
    CLOSED = "closed"
    NOT_APPROVED = "not_approved"


@dataclass(frozen=True, slots=True)
class Extension:
    """A row's extension: how long, and on which of the contest's tasks by
    name, every task when `tasks` is none.
    """

    length: timedelta = timedelta(0)
    tasks: frozenset[str] | None = None

    def on(self, task: str) -> timedelta:
        """How far it moves the row's due and close on `task`."""
        if self.tasks is not None and task not in self.tasks:
            return timedelta(0)
        return self.length


NONE = Extension()


@dataclass(frozen=True, slots=True)
class Openness:
    open: bool
    reason: Closed | None


def released(contest: ContestDefinition, task: str, now: datetime) -> bool:
    """Whether the task, by its name, is released to contestants at `now`."""
    if contest.state is State.DRAFT or now < contest.start:
        return False
    entry = contest.entry(task)
    return entry is not None and now >= contest.release_of(entry)


def visible(contest: ContestDefinition, task: str, now: datetime) -> bool:
    return released(contest, task, now) and contest.state is not State.ARCHIVED


def due_of(contest: ContestDefinition, entry: ContestTask, extension: Extension) -> datetime | None:
    """The row's due on the task, or none when the task has no due."""
    return entry.due + extension.on(entry.id) if entry.due is not None else None


def close_of(contest: ContestDefinition, entry: ContestTask, extension: Extension) -> datetime:
    """The last moment the row may submit to the task, exclusive."""
    return contest.closes_of(entry) + extension.on(entry.id)


def openness(
    contest: ContestDefinition, task: str, now: datetime, extension: Extension = NONE
) -> Openness:
    """Whether one row, with its own `extension`, may submit to the task."""
    entry = contest.entry(task)
    if entry is None or not released(contest, task, now):
        return Openness(False, Closed.NOT_RELEASED)
    if contest.state is State.ARCHIVED:
        return Openness(False, Closed.ARCHIVED)
    if now >= close_of(contest, entry, extension):
        return Openness(False, Closed.CLOSED)
    return Openness(True, None)


def late_days(due: datetime | None, at: datetime) -> int:
    """How many started days of 86,400 seconds a submission made `at` is
    after `due`: 0 on time or with no due, 1 for one second late.
    """
    if due is None or at <= due:
        return 0
    return math.ceil((at - due) / DAY)


def reveal_of(
    contest: ContestDefinition, entry: ContestTask, extensions: Iterable[Extension]
) -> datetime:
    """When the task has closed for every row: its close plus the longest
    extension on it.
    """
    longest = max((extension.on(entry.id) for extension in extensions), default=timedelta(0))
    return contest.closes_of(entry) + max(longest, timedelta(0))


def contest_visible_to(
    contest: ContestDefinition, *, has_session: bool, is_contestant: bool, is_organiser: bool
) -> bool:
    """Whether a reader sees the contest at all. Organisers always do. Anyone
    else sees only a published one: every reader when it is for everyone, a
    reader with a session when it is for the signed-in, and its contestants
    when it is hidden.
    """
    if is_organiser:
        return True
    if contest.state is not State.PUBLISHED:
        return False
    match contest.visibility:
        case ContestVisibility.EVERYONE:
            return True
        case ContestVisibility.SIGNED_IN:
            return has_session
        case ContestVisibility.HIDDEN:
            return is_contestant


def is_running(contest: ContestDefinition, now: datetime) -> bool:
    """Whether the contest is published and between its start and its end."""
    return contest.state is State.PUBLISHED and contest.start <= now < contest.end


def has_started(contest: ContestDefinition, now: datetime) -> bool:
    """Whether the contest has started and is not archived: from then on a
    change to how a task grades asks for confirmation.
    """
    return contest.state is State.PUBLISHED and now >= contest.start
