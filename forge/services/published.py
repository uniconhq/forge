"""What contestants and visitors are shown of a contest and its tasks: the
contest's `contest.yaml` as it stands, and each task as its latest
publication froze it, since a draft saved after that is the organisers' and
not yet anyone else's. Everything is read as the platform: contestants have
no read access to a contest or a task at the forge, and visitors have no
credential at all, so the package applies the contest's visibility and the
release rules itself before it hands anything on.

A task with no publication has nothing to show. A run task's publication
whose plan holds steps done at the save is not the task's until its save
row ends `done` with every run accepted (`forge.services.pipelines`), so
while it is checked, once it is refused, or when its save failed before
its row was written, the task is as the publication before froze it, and
with none before it has nothing to show (`live`). A
task whose latest publication cannot be read as `task.yaml` shows nothing either, which cannot
happen through a save, since only a valid save publishes.

Of a task's files, its readers are served its statement and the files under
its `public/` folder (TASK-FORMAT.md section 1.2), such as a statement's
images, or, for a run task, the files and folders its `show` lists, each
from its time on (SIMPLER-CORE.md 8.4, #487), one by one by its path in the
task, as its latest publication holds it (`public_file`); an upload by its
bytes, never its pointer. Any other path, or one not there, is no such
file, and one larger than `PUBLIC_MOST` is refused with
`PublicFileTooLarge`, since a page shows these whole. A contest whose
`contest.yaml` does not read is no such contest to its readers, the same
answer as one that is not there, so its errors reach nobody but the log.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from forge.db.tables import Grading
from forge.db.tables.gradings import PASSED, SAVE
from forge.domain.content import check_path
from forge.domain.definitions import (
    CONTEST_FILE,
    STATEMENT_FILE,
    TASK_FILE,
    ContestDefinition,
    ContestTask,
    TaskDefinition,
    parse_contest,
    parse_task,
    statement_written,
    worth_of,
)
from forge.domain.errors import InvalidPath, NotFound, PortError, PublicFileTooLarge, Rejected
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.plans import PLAN_PATH, Plan
from forge.domain.publications import Publication
from forge.domain.run_plan import RunPlan
from forge.domain.run_task import RUNS_PATH, Run, RunsFile, file_time
from forge.domain.showing import generation_of
from forge.domain.submissions import Field, fields_of, run_fields_of
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import names, org_accounts

log = get_logger(__name__)

NO_SUCH_CONTEST = names.NO_SUCH_CONTEST
NO_SUCH_TASK = names.NO_SUCH_TASK
NO_SUCH_FILE = "There is no such file."
PUBLIC_FOLDER = "public/"
PUBLIC_MOST = 10 * 1024 * 1024
"""The most bytes of one public file the platform serves a reader."""


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
    def worth(self) -> int | Decimal | None:
        """The most points the task gives in the contest: its entry's `worth`,
        its groups' total on a task that gives points when the entry says
        none, and none on a task that gives no points or that the contest
        does not list.
        """
        if self.entry is None:
            return None
        return worth_of(self.entry, self.definition)


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
        publications = await live(ctx, task)
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


async def live(
    ctx: Context, task: TaskId, made: Sequence[Publication] | None = None
) -> list[Publication]:
    """The task's publications that are its own, oldest first, from `made`
    when the caller has listed them: every one but a run task's that its
    save row has not let take over, its latest attempt not `done` with every
    run accepted, told in the query; and one its note says is checked first
    (`Publication.checked_first`) is held back until such a row passed,
    even with no row at all, as a save that failed after publishing leaves
    it. `NotFound` for no such task.
    """
    if made is None:
        made = await ctx.forge.workspaces.list_publications(task)
    latest = (
        select(Grading.publication_id, PASSED.label("passed"))
        .where(Grading.task_id == task, Grading.pipeline == SAVE)
        .order_by(Grading.publication_id, Grading.attempt.desc())
        .distinct(Grading.publication_id)
        .subquery()
    )
    passed = {
        publication: bool(ok)
        for publication, ok in await ctx.db.execute(
            select(latest.c.publication_id, latest.c.passed)
        )
    }
    return [each for each in made if passed.get(each.id, not each.checked_first)]


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
    tests of the plan, which a per-test input's files are named for; or,
    for a run task (`runs`), the entrant's inputs its run plan declares,
    with no tests, and how many parts each input given in parts has
    (`parts`), its submission laid out as version 6.
    """

    fields: tuple[Field, ...]
    tests: tuple[str, ...]
    runs: bool = False
    parts: Mapping[str, int] = field(default_factory=dict)


FORM_KEPT = timedelta(hours=1)
"""How long this process keeps a publication's form: a publication never
changes, so the time only bounds what is held."""


@dataclass(frozen=True, slots=True)
class RunsHeld:
    """A run task's latest publication as its entrants' results read it:
    its runs in the task's order, and every file it holds.
    """

    runs: tuple[Run, ...]
    files: frozenset[str]


async def runs_held(ctx: Context, published: PublishedTask) -> RunsHeld:
    """The runs and files of a run task's latest publication, read once,
    since a publication never changes. A publication's runs file always
    reads, as its save wrote it, so one that does not fails loudly.
    """
    version = published.publication.version

    async def read() -> RunsHeld:
        found = await ctx.forge.content.read_file(PLATFORM, published.id, RUNS_PATH, at=version)
        listed = await ctx.forge.content.list_files(PLATFORM, published.id, at=version)
        return RunsHeld(RunsFile.from_bytes(found.content).runs, frozenset(listed.tokens))

    key = f"published.runs.{published.id}.{version}"
    return await ctx.memo.remembered(key, FORM_KEPT, read)


async def form(ctx: Context, published: PublishedTask) -> TaskForm:
    """The task's form at its latest publication, read from its plan."""

    async def read() -> TaskForm:
        found = await ctx.forge.content.read_file(
            PLATFORM, published.id, PLAN_PATH, at=published.publication.version
        )
        if published.definition.runs:
            planned = RunPlan.from_bytes(found.content)
            parts = {name: each.parts for name, each in planned.entrant.items() if each.parts}
            fields = run_fields_of(planned.entrant, published.definition.inputs)
            return TaskForm(fields, (), runs=True, parts=parts)
        plan = Plan.from_bytes(found.content)
        fields = fields_of(
            plan.contestant, published.definition.inputs, published.publication.titles
        )
        return TaskForm(fields, plan.tests)

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


async def statement_written_at(ctx: Context, task: TaskId, version: VersionId, key: str) -> bool:
    """Whether the task's `statement.md` at `version` holds a statement, not
    blank and not the starter's placeholder (`statement_written`, M57), read
    as the platform once per process for each `key`, the file's token or a
    publication's id, neither of which ever names other content.
    """

    async def read() -> bool:
        try:
            found = await ctx.forge.content.read_file(PLATFORM, task, STATEMENT_FILE, at=version)
        except NotFound:
            return False
        return statement_written(found.content)

    return await ctx.memo.remembered(f"published.statement.{task}.{key}", FORM_KEPT, read)


@dataclass(frozen=True, slots=True)
class PublicFile:
    """A file a task shows its readers, as its latest publication, or for an
    organiser its head, holds it: its path in the task and its bytes, an
    upload's own bytes rather than its pointer.
    """

    path: str
    content: bytes


def public_path(path: str) -> str:
    """`path` when it names a file plainly, and `NotFound` otherwise: one
    with an empty, `.` or `..` segment or a character `check_path` refuses.
    Whether the task shows it is `public_file`'s.
    """
    try:
        check_path(path)
    except InvalidPath:
        raise NotFound(NO_SUCH_FILE) from None
    return path


def shown_now(
    definition: TaskDefinition, path: str, *, reveal: datetime | None, now: datetime
) -> bool:
    """Whether the task shows its readers the file at `path` now: one under
    `public/`; or for a run task, one its `show` lists, or a folder it is in,
    under `now`, or under `after_close` once the task's reveal has passed.
    `on_release` shows nothing until a release, which Unicon has no action
    for yet, and a file no entry lists is never shown.
    """
    if definition.scoring is None:
        return path.startswith(PUBLIC_FOLDER)
    time = file_time(definition.scoring, path)
    return time == "now" or (time == "after_close" and reveal is not None and reveal <= now)


async def public_file(
    ctx: Context, published: PublishedTask, path: str, *, reveal: datetime | None = None
) -> PublicFile:
    """The file at `path` as the task's latest publication holds it, when
    the task shows it now (`shown_now`, `reveal` being when the task closes
    for every row, which a run task's `after_close` waits for), read as the
    platform, its large-file object's bytes for an upload. The caller has
    checked the reader may read the task and the path with `public_path`.
    `NotFound` for a file not shown or not there, and `PublicFileTooLarge`
    for one over `PUBLIC_MOST` bytes, read no further.
    """
    if not shown_now(published.definition, path, reveal=reveal, now=ctx.now):
        raise NotFound(NO_SUCH_FILE)
    version = published.publication.version
    await ctx.let_go()

    async def listed() -> frozenset[str]:
        files = await ctx.forge.content.list_files(PLATFORM, published.id, at=version)
        return frozenset(files.tokens)

    # What a publication holds never changes, so its listing is read once.
    key = f"published.public.{published.id}.{version}"
    try:
        held = await ctx.memo.remembered(key, FORM_KEPT, listed)
    except NotFound:
        raise NotFound(NO_SUCH_FILE) from None
    if path not in held:
        raise NotFound(NO_SUCH_FILE)
    return await _public_bytes(ctx, published.id, path, version)


async def head_public_file(ctx: Context, task: TaskId, path: str) -> PublicFile:
    """The file at `path` as the task's head holds it, the draft a statement
    being written draws its pictures from, whether or not it was ever
    published, for whoever organises the task: one under `public/`, or, when
    the head's `task.yaml` is a run task's, one its `show` lists at any
    time. The caller has checked who asks. `NotFound` for a task or a file
    not there, or one the task never shows.
    """
    await ctx.let_go()
    try:
        files = await ctx.forge.content.list_files(PLATFORM, task)
    except NotFound:
        raise NotFound(NO_SUCH_TASK) from None
    if path not in files.tokens or not await _shown_at_head(ctx, task, path, files.version):
        raise NotFound(NO_SUCH_FILE)
    return await _public_bytes(ctx, task, path, files.version)


async def _shown_at_head(ctx: Context, task: TaskId, path: str, version: VersionId) -> bool:
    try:
        found = await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=version)
        definition = parse_task(found.content)
    except NotFound, InvalidDefinition:
        return path.startswith(PUBLIC_FOLDER)
    if definition.scoring is None:
        return path.startswith(PUBLIC_FOLDER)
    return file_time(definition.scoring, path) is not None


async def _public_bytes(ctx: Context, task: TaskId, path: str, version: VersionId) -> PublicFile:
    """The file's bytes at the version, an upload's own, refused past
    `PUBLIC_MOST`.
    """
    try:
        content = await ctx.forge.content.read_media(
            PLATFORM, task, path, at=version, max_size=PUBLIC_MOST
        )
    except NotFound:
        raise NotFound(NO_SUCH_FILE) from None
    except Rejected:
        log.info("published.public_file_too_large", task=task, path=path)
        raise PublicFileTooLarge(
            f"The file is larger than the {PUBLIC_MOST // (1024 * 1024)} MB Unicon shows on a "
            "page.",
            limit=PUBLIC_MOST,
        ) from None
    return PublicFile(path, content)


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
