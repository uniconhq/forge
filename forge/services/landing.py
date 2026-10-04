"""What a visitor with no session reads: the contests whose `visibility` is
`public` and that are published, and the statements of their released tasks,
and nothing else. A visitor has no credential, so everything is read as the
platform, and the package applies the contest's visibility and the release
rules before it hands anything on. A contest that is not public, and a task
that is not visible, are no such contest or task, the same answer as one
that is not there. The list of public contests is kept for five seconds by
each process, since anyone may ask for it and it reads every org's contests
at the forge; a contest made public or hidden shows there within that time.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import NotFound
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import ScopeNames
from forge.domain.roles import contest_id_of, contest_scope, task_scope
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, published


@dataclass(frozen=True, slots=True)
class PublicTask:
    """A released task of a public contest, by its name, the label the contest
    gives it and its title.
    """

    task: TaskId
    name: str
    label: str
    title: str


@dataclass(frozen=True, slots=True)
class PublicContest:
    """A public contest: where it is, by the names of its org and itself,
    what it is called, what it says of itself, when it runs, and its
    released tasks.
    """

    contest: ContestId
    where: ScopeNames
    name: str
    description: str
    start: datetime
    end: datetime
    tasks: tuple[PublicTask, ...]


@dataclass(frozen=True, slots=True)
class PublicStatement:
    """A released task of a public contest with its statement."""

    task: PublicTask
    statement: str


PUBLIC_LIST_KEPT = timedelta(seconds=5)


@action
async def contests(ctx: Context) -> tuple[PublicContest, ...]:
    """Every public contest, newest start first, without its tasks, as it
    stood at most five seconds ago.
    """

    async def read() -> tuple[PublicContest, ...]:
        public = [pair for pair in await published.every_contest(ctx) if _public(pair[1])]
        where = await names.places_named(ctx, [contest for contest, _ in public])
        return tuple(
            _contest(contest, where[contest], settings, ())
            for contest, settings in public
            if contest in where
        )

    return await ctx.memo.remembered("landing.contests", PUBLIC_LIST_KEPT, read)


@action
async def contest(ctx: Context, contest: ContestId) -> PublicContest:
    """A public contest with its released tasks. `NotFound` for any other."""
    settings = await _public_settings(ctx, contest, published.NO_SUCH_CONTEST)
    now = ctx.now
    tasks = tuple(
        PublicTask(task.id, task.name, task.label, task.definition.name)
        for task in await published.tasks(ctx, contest, settings)
        if rules.visible(settings, task.definition, now)
    )
    where = await names.scope_names(ctx, contest_scope(contest))
    return _contest(contest, where, settings, tasks)


@action
async def statement(ctx: Context, task: TaskId) -> PublicStatement:
    """The statement of a released task of a public contest. `NotFound` for any
    other task.
    """
    settings = await _public_settings(ctx, contest_id_of(task_scope(task)), published.NO_SUCH_TASK)
    found = await published.task(ctx, task, settings)
    if found is None or not rules.visible(settings, found.definition, ctx.now):
        raise NotFound(published.NO_SUCH_TASK)
    return PublicStatement(
        PublicTask(found.id, found.name, found.label, found.definition.name),
        await published.statement(ctx, found),
    )


def _public(settings: ContestDefinition) -> bool:
    return rules.contest_visible_to(
        settings, has_session=False, is_contestant=False, is_organiser=False
    )


async def _public_settings(ctx: Context, contest: ContestId, missing: str) -> ContestDefinition:
    try:
        settings = await published.contest(ctx, contest)
    except NotFound as exc:
        raise NotFound(missing) from exc
    if not _public(settings):
        raise NotFound(missing)
    return settings


def _contest(
    contest: ContestId,
    where: ScopeNames,
    settings: ContestDefinition,
    tasks: tuple[PublicTask, ...],
) -> PublicContest:
    return PublicContest(
        contest=contest,
        where=where,
        name=settings.name,
        description=settings.description,
        start=settings.start,
        end=settings.end,
        tasks=tasks,
    )
