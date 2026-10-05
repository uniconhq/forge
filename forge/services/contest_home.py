"""What a signed-in person reads of the contests they may see: the list of
them, a contest's home and a task's page. It is read from the forge as the
platform, with the contest's visibility and the release rules applied first
(`published`). The list starts from every published contest as this process
read it at most half a minute ago, shared by everyone, and is then filtered
for the person; a home and a task's page are read live. Each lets go of its
connection to the database before it reads the forge.

A person sees a published contest that is `public` or `signed-in`, and a
`hidden` one only once they are its approved contestant; organisers see
their own contests whatever their state. The home carries the contest's
dates, the person's own registration, what the register form needs to know,
the person's own deadline, which is the contest's end plus their extension,
the server's clock so a countdown agrees with it, and the tasks released to
them, the ones visible now. A task's page gives its statement and its limits
and nothing else of what the task holds, only once the task is visible. A task that is not, or a
contest the person may not see, is no such task or contest, the same answer
as one that is not there at all.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.domain import registration
from forge.domain import release as rules
from forge.domain.definitions import (
    ContestantInput,
    ContestVisibility,
    Limits,
    RegistrationMode,
    State,
)
from forge.domain.errors import NotFound
from forge.domain.identity import User
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import ScopeNames
from forge.domain.registration import Status
from forge.domain.roles import contest_id_of, contest_scope, task_scope
from forge.domain.sessions import Session
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import contestants, invites, names, published, release
from forge.services.contestants import Registration
from forge.services.release import TaskRelease


@dataclass(frozen=True, slots=True)
class ContestSummary:
    """A contest in the list: where it is, by the names of its org and
    itself, what it is called, when it runs, and the person's own
    registration status, if they have one.
    """

    contest: ContestId
    where: ScopeNames
    name: str
    start: datetime
    end: datetime
    visibility: ContestVisibility
    status: Status | None


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """A task released to the person, as the contest's home lists it: its
    name, the label and points the contest gives it, its title, and whether
    they may submit to it now.
    """

    task: TaskId
    name: str
    label: str
    title: str
    points: int | None
    release: TaskRelease


@dataclass(frozen=True, slots=True)
class ContestHome:
    """A contest's home for one person. `organises` says they hold a role at
    the contest, which keeps them from entering it; `registration_open` says
    whether the window is open now, and `invite_only` and `asks_code` what
    the register form needs; `deadline` is the contest's end plus the person's own
    extension, and `now` the server's clock when this was read. `where` is
    the contest by the names of its org and itself.
    """

    contest: ContestId
    where: ScopeNames
    name: str
    description: str
    start: datetime
    end: datetime
    state: State
    submissions_closed: bool
    registration: Registration | None
    organises: bool
    registration_open: bool
    invite_only: bool
    asks_code: bool
    deadline: datetime
    now: datetime
    tasks: tuple[TaskEntry, ...]


@dataclass(frozen=True, slots=True)
class TaskPage:
    """A task as a person reads it: its statement, the limits a submit is
    checked against, and the inputs a contestant gives, which the submit
    panel is built from.
    """

    task: TaskId
    name: str
    label: str
    title: str
    points: int | None
    statement: str
    limits: Limits
    inputs: tuple[ContestantInput, ...]
    release: TaskRelease


@action
async def contests(ctx: Context, session: Session) -> tuple[ContestSummary, ...]:
    """Every contest the signed-in person sees, newest start first, each with
    their own registration status. Organisers find their own contests from
    their orgs, so this lists what a contestant sees.
    """
    rows = {
        row.contest_id: row
        for row in (
            await ctx.db.execute(select(Contestant).where(Contestant.user_id == session.user_id))
        ).scalars()
    }
    invited = await invites.accepted_places(ctx, session.user_id)
    await ctx.let_go()
    found = []
    every = await published.every_contest_kept(ctx)
    where = await names.places_named(ctx, [contest for contest, _ in every])
    for contest, settings in every:
        row = rows.get(contest)
        if contest in where and rules.contest_visible_to(
            settings,
            has_session=True,
            is_contestant=_approved(row) or contest in invited,
            is_organiser=False,
        ):
            found.append(
                ContestSummary(
                    contest=contest,
                    where=where[contest],
                    name=settings.name,
                    start=settings.start,
                    end=settings.end,
                    visibility=settings.visibility,
                    status=Status(row.status) if row is not None else None,
                )
            )
    return tuple(found)


@action
async def home(ctx: Context, session: Session, contest: ContestId) -> ContestHome:
    """The contest's home for the signed-in person. `NotFound` for a contest
    they may not see.
    """
    settings, person = await release.seen(ctx, session, contest)
    await ctx.let_go()
    now = ctx.now
    extension = contestants.time_extension(person.row)
    tasks = tuple(
        TaskEntry(
            task=task.id,
            name=task.name,
            label=task.label,
            title=task.definition.name,
            points=task.points,
            release=release.of(settings, task.definition, now, extension),
        )
        for task in await published.tasks(ctx, contest, settings)
        if rules.visible(settings, task.definition, now)
    )
    entry = settings.registration
    return ContestHome(
        contest=contest,
        where=await names.scope_names(ctx, contest_scope(contest)),
        name=settings.name,
        description=settings.description,
        start=settings.start,
        end=settings.end,
        state=settings.state,
        submissions_closed=settings.submissions_closed,
        registration=(
            contestants.registration_of(person.row, _session_user(session))
            if person.row is not None
            else None
        ),
        organises=person.organises,
        registration_open=registration.window_open(entry, now),
        invite_only=entry.mode is RegistrationMode.INVITE_ONLY,
        asks_code=entry.eligibility.invite_code is not None,
        deadline=settings.end + extension,
        now=now,
        tasks=tasks,
    )


@action
async def task(ctx: Context, session: Session, task: TaskId) -> TaskPage:
    """The task's statement, limits and contestant inputs for the signed-in
    person. `NotFound`
    for a task that is not visible to them, or in a contest they may not see.
    """
    contest = contest_id_of(task_scope(task))
    try:
        settings, person = await release.seen(ctx, session, contest)
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    found = await published.task(ctx, task, settings)
    await ctx.let_go()
    now = ctx.now
    if found is None or not rules.visible(settings, found.definition, now):
        raise NotFound(published.NO_SUCH_TASK)
    return TaskPage(
        task=task,
        name=found.name,
        label=found.label,
        title=found.definition.name,
        points=found.points,
        statement=await published.statement(ctx, found),
        limits=found.definition.limits,
        inputs=found.definition.inputs.contestant,
        release=release.of(settings, found.definition, now, contestants.time_extension(person.row)),
    )


def _session_user(session: Session) -> User:
    return User(id=session.user_id, username=session.username)


def _approved(row: Contestant | None) -> bool:
    return row is not None and row.status == Status.APPROVED
