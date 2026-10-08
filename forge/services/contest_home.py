"""What a signed-in person reads of the contests they may see: the list of
them, a contest's home and a task's page. It is read from the forge as the
platform, with the contest's visibility and the release rules applied first
(`published`). The list starts from every published contest as this process
read it at most half a minute ago, shared by everyone, and is then filtered
for the person; a home and a task's page are read live. Each lets go of its
connection to the database before it reads the forge.

A person sees a published contest that is for `everyone` or the
`signed-in`, and a `hidden` one once they are its approved contestant, or
have accepted an invite to it and not registered; organisers see their own
contests whatever their state. The home carries the contest's dates, the
person's own registration, what the register form needs to know, the
server's clock so a countdown agrees with it, and the tasks released to
them, the ones visible now, each with its label, its worth and when it
falls due and closes for the person's row, their extension on it included.
A task's page gives its statement, its submission caps and the form of the
inputs a contestant gives, and nothing else of what the task holds, only
once the task is visible. A task that is not, or a contest the person may
not see, is no such task or contest, the same answer as one that is not
there at all.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.domain import registration
from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition, ContestVisibility, State, Submissions
from forge.domain.errors import NotFound
from forge.domain.identity import User
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import ScopeNames
from forge.domain.registration import Status
from forge.domain.release import Closed, Extension, close_of, due_of
from forge.domain.roles import contest_id_of, contest_scope, task_scope
from forge.domain.sessions import Session
from forge.domain.submissions import Field
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import contestants, invites, names, published, release
from forge.services.contestants import Registration
from forge.services.published import PublishedTask
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
    name, the label its place in the contest gives it, its title, its worth,
    whether they may submit to it now, and when it falls due, if it does,
    and closes for their row.
    """

    task: TaskId
    name: str
    label: str
    title: str
    worth: int | float | None
    release: TaskRelease
    due: datetime | None
    closes: datetime | None


@dataclass(frozen=True, slots=True)
class ContestHome:
    """A contest's home for one person. `organises` says they hold a role at
    the contest, which keeps them from entering it; `registration_open` says
    whether the window is open now, and `invite_only` and `asks_code` what
    the register form needs; `now` is the server's clock when this was read.
    `where` is the contest by the names of its org and itself.
    """

    contest: ContestId
    where: ScopeNames
    name: str
    description: str
    start: datetime
    end: datetime
    state: State
    registration: Registration | None
    organises: bool
    registration_open: bool
    invite_only: bool
    asks_code: bool
    now: datetime
    tasks: tuple[TaskEntry, ...]


@dataclass(frozen=True, slots=True)
class TaskPage:
    """A task as a person reads it: its statement, the caps a submit is
    counted against, the inputs a contestant gives, which the submit panel
    is built from, when it falls due and closes for the person's row, and
    how many of the row's submissions it may mark for the `marked` boards,
    none unless such a board covers the task and the person is an approved
    contestant.
    """

    task: TaskId
    name: str
    label: str
    title: str
    worth: int | float | None
    statement: str
    submissions: Submissions
    inputs: tuple[Field, ...]
    release: TaskRelease
    due: datetime | None
    closes: datetime | None
    marks: int | None = None


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
            is_contestant=_approved(row) or (row is None and contest in invited),
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
    extension = await release.row_extension(ctx, contest, session.user_id, person)
    await ctx.let_go()
    now = ctx.now
    tasks = tuple(
        TaskEntry(
            task=task.id,
            name=task.name,
            label=task.label or task.name,
            title=task.definition.name,
            worth=task.worth,
            release=_to(person, release.of(settings, task.name, now, extension)),
            due=_due(settings, task, extension),
            closes=_closes(settings, task, extension),
        )
        for task in await published.tasks(ctx, contest, settings)
        if rules.visible(settings, task.name, now)
    )
    entry = settings.registration
    return ContestHome(
        contest=contest,
        where=await names.scope_names(ctx, contest_scope(contest)),
        name=settings.name,
        description=settings.description or "",
        start=settings.start,
        end=settings.end,
        state=settings.state,
        registration=(
            contestants.registration_of(person.row, _session_user(session))
            if person.row is not None
            else None
        ),
        organises=person.organises,
        registration_open=registration.window_open(entry, now),
        invite_only=entry.invite_only,
        asks_code=entry.code is not None,
        now=now,
        tasks=tasks,
    )


@action
async def task(ctx: Context, session: Session, task: TaskId) -> TaskPage:
    """The task's statement, submission caps and contestant inputs for the
    signed-in person. `NotFound` for a task that is not visible to them, or
    in a contest they may not see.
    """
    contest = contest_id_of(task_scope(task))
    try:
        settings, person = await release.seen(ctx, session, contest)
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    found = await published.task(ctx, task, settings)
    extension = await release.row_extension(ctx, contest, session.user_id, person)
    await ctx.let_go()
    now = ctx.now
    if found is None or not rules.visible(settings, found.name, now):
        raise NotFound(published.NO_SUCH_TASK)
    form = await published.form(ctx, found)
    return TaskPage(
        task=task,
        name=found.name,
        label=found.label or found.name,
        title=found.definition.name,
        worth=found.worth,
        statement=await published.statement(ctx, found),
        submissions=found.definition.submissions,
        inputs=form.fields,
        release=_to(person, release.of(settings, found.name, now, extension)),
        due=_due(settings, found, extension),
        closes=_closes(settings, found, extension),
        marks=(
            settings.marks_of(found.entry)
            if found.entry is not None and _approved(person.row)
            else None
        ),
    )


def _due(
    settings: ContestDefinition, found: PublishedTask, extension: Extension
) -> datetime | None:
    if found.entry is None:
        return None
    return due_of(settings, found.entry, extension)


def _closes(
    settings: ContestDefinition, found: PublishedTask, extension: Extension
) -> datetime | None:
    if found.entry is None:
        return None
    return close_of(settings, found.entry, extension)


def _session_user(session: Session) -> User:
    return User(id=session.user_id, username=session.username)


def _approved(row: Contestant | None) -> bool:
    return row is not None and row.status == Status.APPROVED


def _to(person: release.Reader, found: TaskRelease) -> TaskRelease:
    """Where a task stands for `person`: not open, as `not_approved`, where
    it is open but they hold no approved row to submit from.
    """
    if found.open and not _approved(person.row):
        return replace(found, open=False, closed=Closed.NOT_APPROVED)
    return found
