"""Whether a contestant sees a task and whether they may submit to it,
worked out from the settings and the clock on every read. Nobody releases a
task and nothing fires when the moment comes.

A task is released once its contest has been published and has started,
its own `release_at` has passed if it has one, and it is not `hidden`. An
archived contest was published before, so its tasks stay released. A task
is visible while it is released and its contest is not archived, so
contestants keep the statement and their results after the end. It is open
while it is visible, the contest's end plus the contestant's own extension
has not passed, and `submissions_closed` is off. A start or a release time
counts from its own instant; the end is the first instant that is closed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from forge.domain.definitions import (
    ContestDefinition,
    ContestVisibility,
    State,
    TaskDefinition,
)


class Closed(StrEnum):
    """Why a task is not open, the first of these that applies."""

    NOT_RELEASED = "not_released"
    ARCHIVED = "archived"
    ENDED = "ended"
    SUBMISSIONS_CLOSED = "submissions_closed"


@dataclass(frozen=True, slots=True)
class Openness:
    open: bool
    reason: Closed | None


def released(contest: ContestDefinition, task: TaskDefinition, now: datetime) -> bool:
    if contest.state is State.DRAFT or now < contest.start:
        return False
    if task.release_at is not None and now < task.release_at:
        return False
    return not task.hidden


def visible(contest: ContestDefinition, task: TaskDefinition, now: datetime) -> bool:
    return released(contest, task, now) and contest.state is not State.ARCHIVED


def openness(
    contest: ContestDefinition,
    task: TaskDefinition,
    now: datetime,
    extension: timedelta = timedelta(0),
) -> Openness:
    """Whether one contestant, with their own `extension`, may submit."""
    if not released(contest, task, now):
        return Openness(False, Closed.NOT_RELEASED)
    if contest.state is State.ARCHIVED:
        return Openness(False, Closed.ARCHIVED)
    if now >= contest.end + extension:
        return Openness(False, Closed.ENDED)
    if contest.submissions_closed:
        return Openness(False, Closed.SUBMISSIONS_CLOSED)
    return Openness(True, None)


def contest_visible_to(
    contest: ContestDefinition, *, has_session: bool, is_contestant: bool, is_organiser: bool
) -> bool:
    """Whether a reader sees the contest at all. Organisers always do. Anyone
    else sees only a published one: every reader when it is public, a
    reader with a session when it is signed-in, and its contestants when it
    is hidden.
    """
    if is_organiser:
        return True
    if contest.state is not State.PUBLISHED:
        return False
    match contest.visibility:
        case ContestVisibility.PUBLIC:
            return True
        case ContestVisibility.SIGNED_IN:
            return has_session
        case ContestVisibility.HIDDEN:
            return is_contestant


def is_running(contest: ContestDefinition, now: datetime) -> bool:
    """Whether the contest is published and between its start and its end."""
    return contest.state is State.PUBLISHED and contest.start <= now < contest.end
