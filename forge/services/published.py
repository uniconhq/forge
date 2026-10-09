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
from datetime import timedelta

from forge.domain.definitions import (
    CONTEST_FILE,
    DEFAULT_WORTH,
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
from forge.domain.plans import PLAN_PATH, Plan
from forge.domain.publications import Publication
from forge.domain.showing import generation_of
from forge.domain.submissions import Field, fields_of
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import names, org_accounts

log = get_logger(__name__)

NO_SUCH_CONTEST = names.NO_SUCH_CONTEST
NO_SUCH_TASK = names.NO_SUCH_TASK


@dataclass(frozen=True, slots=True)
class PublishedTask:
    """A task as its latest publication froze it, by its name, with its
    entry in the contest's `tasks` list and the label its place there gives
    it, when the contest lists it, and every publication of it, oldest
    first.
    """

    id: TaskId
    name: str
    publication: Publication
    definition: TaskDefinition
    entry: ContestTask | None
    label: str | None = None
    publications: tuple[Publication, ...] = ()

    def generation(self, publication: Publication) -> int:
        """`showing.generation_of` for one of the task's publications."""
        return generation_of(
            [(each.number, each.grading_changed) for each in self.publications],
            publication.number,
        )

    @property
    def worth(self) -> int | float | None:
        """The most points the task gives in the contest: its entry's `worth`,
        100 on a task that gives points when the entry says none, and none on
        a task that gives no points or that the contest does not list.
        """
        if self.entry is None or not self.definition.gives_points:
            return None
        return self.entry.worth if self.entry.worth is not None else DEFAULT_WORTH


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
    ctx: Context, task: TaskId, settings: ContestDefinition | None = None, name: str | None = None
) -> PublishedTask | None:
    """The task as its latest publication froze it, placed in the contest's
    `tasks` list when `settings` are given, or none while it has no
    publication, no name or is not there at all. `name` saves reading it
    again when the caller has it. The connection is let go of before the
    forge is read.
    """
    if name is None:
        name = (await names.names_of(ctx, [task])).get(task)
        if name is None:
            return None
    await ctx.let_go()
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
    every = tuple(publications)
    if settings is None:
        return PublishedTask(task, name, latest, definition, None, publications=every)
    return PublishedTask(
        task,
        name,
        latest,
        definition,
        settings.entry(name),
        settings.label_of(name),
        every,
    )


async def tasks(
    ctx: Context, contest: ContestId, settings: ContestDefinition
) -> list[PublishedTask]:
    """Every task of the contest with a publication, in the order its `tasks`
    list gives, and after those the rest by name. The connection is let go of
    once the names are read, before each task is read at the forge.
    """
    named = await names.named(ctx, await ctx.forge.content.list_tasks(PLATFORM, contest))
    await ctx.let_go()
    found = [
        published
        for each in named
        if (published := await task(ctx, TaskId(each.id), settings, each.name)) is not None
    ]
    order = {entry.id: index for index, entry in enumerate(settings.tasks)}
    return sorted(
        found, key=lambda published: (order.get(published.name, len(order)), published.name)
    )


@dataclass(frozen=True, slots=True)
class TaskForm:
    """What a submission to a task is made of: the inputs the contestant
    gives, as its plan declares them with the task's form details, and the
    tests of the plan, which a per-test input's files are named for.
    """

    fields: tuple[Field, ...]
    tests: tuple[str, ...]


FORM_KEPT = timedelta(hours=1)
"""How long this process keeps a publication's form: a publication never
changes, so the time only bounds what is held."""


async def form(ctx: Context, published: PublishedTask) -> TaskForm:
    """The task's form at its latest publication, read from its plan."""

    async def read() -> TaskForm:
        found = await ctx.forge.content.read_file(
            PLATFORM, published.id, PLAN_PATH, at=published.publication.version
        )
        plan = Plan.from_bytes(found.content)
        return TaskForm(fields_of(plan.contestant, published.definition.inputs), plan.tests)

    key = f"published.form.{published.id}.{published.publication.version}"
    return await ctx.memo.remembered(key, FORM_KEPT, read)


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


EVERY_CONTEST_KEPT = timedelta(seconds=30)
EVERY_CONTEST = "published.every_contest"


async def every_contest_kept(ctx: Context) -> list[tuple[ContestId, ContestDefinition]]:
    """`every_contest` as this process read it at most `EVERY_CONTEST_KEPT`
    ago, for the lists of contests people open: it costs the forge two reads
    an org whoever asks, and is read as the platform, so one answer serves
    everybody, and each person's own filtering happens after. A change to a
    contest's settings saved through this process shows at once
    (`forget_contests`); one saved through another shows within that time.
    """
    return await ctx.memo.remembered(EVERY_CONTEST, EVERY_CONTEST_KEPT, lambda: every_contest(ctx))


def forget_contests(ctx: Context) -> None:
    """Drop this process's copy of `every_contest`, after a contest's settings
    changed at the forge.
    """
    ctx.memo.forget(EVERY_CONTEST)


async def every_contest(ctx: Context) -> list[tuple[ContestId, ContestDefinition]]:
    """Every contest of every org the platform made, with its settings, newest
    start first, read live. An org or a contest the forge fails on is left
    out and logged, so one broken place does not hide the rest. The list of
    orgs is read first and the connection let go of, so the two forge reads
    an org hold none.
    """
    found: list[tuple[ContestId, ContestDefinition]] = []
    orgs = await org_accounts.org_ids(ctx)
    await ctx.let_go()
    for org in orgs:
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
