"""What contestants and visitors are shown of a contest and its tasks: the
contest's `contest.yaml` as it stands, and each task as its latest
publication froze it, since a draft saved after that is the organisers' and
not yet anyone else's. Everything is read as the platform: contestants have
no read access to a contest or a task at the forge, and visitors have no
credential at all, so the package applies the contest's visibility and the
release rules itself before it hands anything on.

A task with no publication has nothing to show. A task whose latest
publication cannot be read as `task.yaml` shows nothing either, which cannot
happen through a save, since only a valid save publishes. A contest whose
`contest.yaml` does not read is no such contest to its readers, the same
answer as one that is not there, so its errors reach nobody but the log.
"""

from dataclasses import dataclass

from forge.domain.definitions import (
    CONTEST_FILE,
    STATEMENT_FILE,
    TASK_FILE,
    ContestDefinition,
    ContestTask,
    TaskDefinition,
    parse_contest,
    parse_task,
)
from forge.domain.errors import NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.publications import Publication
from forge.domain.roles import task_scope
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import org_accounts

log = get_logger(__name__)

NO_SUCH_CONTEST = "There is no such contest."
NO_SUCH_TASK = "There is no such task."


@dataclass(frozen=True, slots=True)
class PublishedTask:
    """A task as its latest publication froze it, with its place in the
    contest's `tasks` list when the contest gives it one.
    """

    id: TaskId
    publication: Publication
    definition: TaskDefinition
    entry: ContestTask | None

    @property
    def name(self) -> str:
        return str(task_scope(self.id).task)

    @property
    def label(self) -> str:
        """What the contest calls the task, or its name when it says nothing."""
        return self.entry.label if self.entry is not None else self.name

    @property
    def points(self) -> int | None:
        return self.entry.points if self.entry is not None else None


async def contest(ctx: Context, contest: ContestId) -> ContestDefinition:
    """The contest's settings as they stand. `NotFound` when there is no such
    contest.
    """
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
    except NotFound as exc:
        raise NotFound(NO_SUCH_CONTEST) from exc
    try:
        return parse_contest(found.content)
    except InvalidDefinition as exc:
        log.warning("published.contest_invalid", contest=contest, problems=len(exc.errors))
        raise NotFound(NO_SUCH_CONTEST) from exc


async def task(
    ctx: Context, task: TaskId, settings: ContestDefinition | None = None
) -> PublishedTask | None:
    """The task as its latest publication froze it, placed in the contest's
    `tasks` list when `settings` are given, or none while it has no
    publication or is not there at all.
    """
    try:
        publications = await ctx.forge.workspaces.list_publications(task)
    except NotFound:
        return None
    if not publications:
        return None
    latest = publications[-1]
    found = await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=latest.version)
    try:
        definition = parse_task(found.content)
    except InvalidDefinition:
        return None
    entries = {entry.id: entry for entry in settings.tasks} if settings is not None else {}
    return PublishedTask(task, latest, definition, entries.get(str(task_scope(task).task)))


async def tasks(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> list[PublishedTask]:
    """Every task of the contest with a publication, in the order its `tasks`
    list gives, and after those the rest by name.
    """
    found = [
        published
        for task_id in await ctx.forge.content.list_tasks(PLATFORM, contest)
        if (published := await task(ctx, task_id, settings)) is not None
    ]
    order = {entry.id: index for index, entry in enumerate(settings.tasks)}
    return sorted(
        found, key=lambda published: (order.get(published.name, len(order)), published.name)
    )


async def statement(ctx: Context, published: PublishedTask) -> str:
    """The task's statement at its latest publication, or nothing when that
    publication has none.
    """
    try:
        found = await ctx.forge.content.read_file(
            PLATFORM, published.id, STATEMENT_FILE, at=published.publication.version
        )
    except NotFound:
        return ""
    return found.content.decode("utf-8", errors="replace")


async def every_contest(ctx: Context) -> list[tuple[ContestId, ContestDefinition]]:
    """Every contest of every org the platform made, with its settings, newest
    start first. An org or a contest the forge fails on is left out and
    logged, so one broken place does not hide the rest.
    """
    found: list[tuple[ContestId, ContestDefinition]] = []
    for org in await org_accounts.org_names(ctx):
        try:
            contests = await ctx.forge.content.list_contests(PLATFORM, org)
        except PortError as exc:
            log.warning("published.org_unreadable", org=org, error=type(exc).__name__)
            continue
        for contest_id in contests:
            try:
                found.append((contest_id, await contest(ctx, contest_id)))
            except PortError as exc:
                log.warning(
                    "published.contest_unreadable", contest=contest_id, error=type(exc).__name__
                )
    return sorted(found, key=lambda pair: (pair[1].start, pair[0]), reverse=True)
