"""What a signed-in person reads of the contests they may see: the list of
them, a contest's home and a task's page. It is read from the forge as the
platform, with the contest's visibility and the release rules applied first
(`published`). The list starts from every published contest as this process
read it at most half a minute ago, shared by everyone, and is then filtered
for the person; a home and a task's page are read live. Each lets go of its
connection to the database before it reads the forge.

A person sees a published contest that is for `everyone` or the
`signed-in`, and a `hidden` one once they are in an approved entrant of it,
or have accepted an organiser's invite to it and are in no entrant there;
organisers see their own contests whatever their state. The home carries the
contest's dates, the person's registration panel (`entrants.panel`): their
entrant and whether and how they may register; the server's clock so a
countdown agrees with it, and the tasks released to them, the ones visible
now, each with its label, its worth and when it falls due and closes for
their entrant, its extension on it included, and, for a member of an
approved entrant, the entrant's points on it on each board an entrant sees,
in the file's order (`boards`), each as that board counts the task, from the
same scoring every board reads (`boards.own_points`): a group whose results
are not shown yet counts only as pending, with the time it is shown, so the
home never shows a number a board holds back.
A task's page gives its statement, its submission caps and the form of the
inputs a contestant gives, and nothing else of what the task holds, only
once the task is visible. A task that is not, or a contest the person may
not see, is no such task or contest, the same answer as one that is not
there at all.

Beside its statement, the files under a task's `public/` are served one by
one (`public_file`), to whoever reads its page, with the same checks, and to
whoever holds a role reaching the task, who reads every file of it as an
organiser anyway, released or not, so a statement shows its images while it
is being written.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from forge.db.tables import Entrant
from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition, ContestVisibility, State, Submissions
from forge.domain.errors import NotFound
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import EntrantOwner, ScopeNames
from forge.domain.registration import Status
from forge.domain.release import Closed, Extension, close_of, due_of
from forge.domain.roles import Role, contest_id_of, contest_scope, holds, task_scope
from forge.domain.sessions import Session
from forge.domain.submissions import Field
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import boards, entrants, invites, names, published, release
from forge.services.boards import BoardPoints
from forge.services.entrants import Mine
from forge.services.published import PublicFile, PublishedTask
from forge.services.release import TaskRelease


@dataclass(frozen=True, slots=True)
class ContestSummary:
    """A contest in the list: where it is, by the names of its org and
    itself, what it is called, when it runs, and the status of the person's
    entrant, if they are in one.
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
    whether they may submit to it now, when it falls due, if it does, and
    closes for their row, and, for a member of an approved entrant, their
    row's points on it on each board of the home's `boards`, in that order
    (`boards.own_points`): shown now, as that board counts the task, and
    pending until the task's reveal; empty for anyone else.
    """

    task: TaskId
    name: str
    label: str
    title: str
    worth: int | Decimal | None
    release: TaskRelease
    due: datetime | None
    closes: datetime | None
    points: tuple[BoardPoints, ...] = ()


@dataclass(frozen=True, slots=True)
class ContestHome:
    """A contest's home for one person. `organises` says they hold a role at
    the contest, which keeps them from entering it; `registration` is their
    registration panel: their entrant, if any, and whether and how they may
    register; `now` is the server's clock when this was read. `where` is the
    contest by the names of its org and itself. `boards` names the
    leaderboards a member of an approved entrant sees, in the file's order,
    which each task's `points` follow; empty for anyone else.
    """

    contest: ContestId
    where: ScopeNames
    name: str
    description: str
    start: datetime
    end: datetime
    state: State
    registration: Mine
    organises: bool
    now: datetime
    tasks: tuple[TaskEntry, ...]
    boards: tuple[str, ...] = ()


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
    worth: int | Decimal | None
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
    the status of their entrant. Organisers find their own contests from
    their orgs, so this lists what a contestant sees.
    """
    statuses = await entrants.statuses(ctx, session.user_id)
    invited = await invites.accepted_places(ctx, session.user_id)
    await ctx.let_go()
    found = []
    every = await published.every_contest_kept(ctx)
    where = await names.places_named(ctx, [contest for contest, _ in every])
    for contest, settings in every:
        status = statuses.get(contest)
        if contest in where and rules.contest_visible_to(
            settings,
            has_session=True,
            is_contestant=status is Status.APPROVED or (status is None and contest in invited),
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
                    status=status,
                )
            )
    return tuple(found)


@action
async def home(ctx: Context, session: Session, contest: ContestId) -> ContestHome:
    """The contest's home for the signed-in person. `NotFound` for a contest
    they may not see.
    """
    settings, person = await release.seen(ctx, session, contest)
    extension = release.row_extension(person)
    await ctx.let_go()
    now = ctx.now
    shown = [
        task
        for task in await published.tasks(ctx, contest, settings)
        if rules.visible(settings, task.name, now)
    ]
    points: dict[str, tuple[BoardPoints, ...]] = {}
    seen: tuple[str, ...] = ()
    if person.entrant is not None and _approved(person.entrant):
        owner = EntrantOwner(person.entrant.id)
        points = await boards.own_points(ctx, contest, settings, shown, owner)
        seen = tuple(board.name for board in boards.entrants_boards(settings))
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
            points=points.get(task.name, ()),
        )
        for task in shown
    )
    panel = await entrants.panel(ctx, session, contest, settings, person)
    return ContestHome(
        contest=contest,
        where=await names.scope_names(ctx, contest_scope(contest)),
        name=settings.name,
        description=settings.description or "",
        start=settings.start,
        end=settings.end,
        state=settings.state,
        registration=panel,
        organises=person.organises,
        now=now,
        tasks=tasks,
        boards=seen,
    )


@action
async def task(ctx: Context, session: Session, task: TaskId) -> TaskPage:
    """The task's statement, submission caps and contestant inputs for the
    signed-in person. `NotFound` for a task that is not visible to them, or
    in a contest they may not see.
    """
    settings, person, found = await _visible(ctx, session, task)
    extension = release.row_extension(person)
    now = ctx.now
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
            if found.entry is not None and _approved(person.entrant)
            else None
        ),
    )


@action
async def public_file(ctx: Context, session: Session, task: TaskId, path: str) -> PublicFile:
    """A file under the task's `public/`, by its path in the task: as its
    latest publication holds it, for a signed-in person who reads the task's
    page (`task`); as its head holds it, the draft the statement's preview
    draws, published or not, for one who holds a role reaching the task,
    whether or not it is released. `NotFound` for a task neither, a path
    outside `public/` or not there; `PublicFileTooLarge` for one larger than
    a page is served.
    """
    path = published.public_path(path)
    if await _organises(ctx, session, task):
        return await published.head_public_file(ctx, task, path)
    _, _, found = await _visible(ctx, session, task)
    return await published.public_file(ctx, found, path)


async def _visible(
    ctx: Context, session: Session, task: TaskId
) -> tuple[ContestDefinition, release.Reader, PublishedTask]:
    """The contest's settings, the person as its reads see them, and the task
    at its latest publication, once it is visible to them. `NotFound`
    otherwise, the same as for a task that is not there.
    """
    contest = contest_id_of(task_scope(task))
    try:
        settings, person = await release.seen(ctx, session, contest)
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    found = await published.task(ctx, task, settings)
    await ctx.let_go()
    if found is None or not rules.visible(settings, found.name, ctx.now):
        raise NotFound(published.NO_SUCH_TASK)
    return settings, person, found


async def _organises(ctx: Context, session: Session, task: TaskId) -> bool:
    """Whether the person holds a role at the task, its contest or its org,
    which lets them read every file of it.
    """
    await ctx.let_go()
    grants = await ctx.forge.orgs.roles_of_user(session.user_id)
    return holds(grants, task_scope(task), Role.OBSERVER)


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


def _approved(row: Entrant | None) -> bool:
    return row is not None and row.status == Status.APPROVED


def _to(person: release.Reader, found: TaskRelease) -> TaskRelease:
    """Where a task stands for `person`: not open, as `not_approved`, where
    it is open but they are in no approved entrant to submit from.
    """
    if found.open and not _approved(person.entrant):
        return replace(found, open=False, closed=Closed.NOT_APPROVED)
    return found
