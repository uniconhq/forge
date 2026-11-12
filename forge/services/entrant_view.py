"""What contestants and visitors are shown of a contest and its tasks: the
contest's `contest.yaml` as it stands, and each task as its latest
publication froze it, since a draft saved after that is the organisers' and
not yet anyone else's. Everything is read as the platform: contestants have
no read access to a contest or a task at the forge, and visitors have no
credential at all, so the package applies the contest's visibility and the
release rules itself before it hands anything on.

A task with no publication has nothing to show. A publication whose plan
holds steps done at the save is not the task's until its save row ends
`done` with every run accepted (`forge.services.pipelines`), so while it is
checked, once it is refused, or when its save failed before its row was
written, the task is as the publication before froze it, and with none
before it has nothing to show (`live`). A task whose latest publication
cannot be read as `task.yaml` shows nothing either, which cannot happen
through a save, since only a valid save publishes.

Of a task's files, its readers are served its statement and the files and
folders its `show` lists, such as a statement's images, each from its time
on (SIMPLER-CORE.md 8.4, #487), one by one by its path in the task, as its
latest publication holds it (`public_file`); an upload by its bytes, never
its pointer. Any other path, or one not there, is no such
file, and one larger than `PUBLIC_MOST` is refused with
`PublicFileTooLarge`, since a page shows these whole. A contest whose
`contest.yaml` does not read is no such contest to its readers, the same
answer as one that is not there, so its errors reach nobody but the log.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from forge.db.tables import Grading, Release
from forge.db.tables.gradings import PASSED, SAVE
from forge.db.tables.releases import RELEASED
from forge.domain import run_showing
from forge.domain.blocks.evaluate import Column
from forge.domain.checks import check_only
from forge.domain.content import check_path
from forge.domain.definitions import (
    CONTEST_FILE,
    PUBLIC_FOLDER,
    STATEMENT_FILE,
    TASK_FILE,
    ContestDefinition,
    ContestTask,
    TaskDefinition,
    parse_contest,
    parse_task,
    statement_written,
)
from forge.domain.errors import InvalidPath, NotFound, PortError, PublicFileTooLarge, Rejected
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, VersionId, WorkflowId
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.publication_record import Publication
from forge.domain.run_plan import RunPlan
from forge.domain.run_results import compilers
from forge.domain.run_task import RUNS_PATH, Run, RunsFile
from forge.domain.submissions import Field, run_fields_of
from forge.domain.types import Type
from forge.domain.uploads import Door
from forge.domain.workflow_definition import (
    WORKFLOW_FILE,
    RunOutput,
    RunWorkflow,
    parse_run_workflow,
)
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import names, org_accounts

log = get_logger(__name__)

NO_SUCH_CONTEST = names.NO_SUCH_CONTEST
NO_SUCH_TASK = names.NO_SUCH_TASK
NO_SUCH_FILE = "There is no such file."
NO_SUCH_INPUT = "There is no such input."
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
    when the caller has listed them: every one but one that its
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
    """What a submission to a task is made of: the entrant's inputs its
    plan declares, with the task's form details, and how many parts each
    input given in parts has (`parts`), its submission laid out as version
    6.
    """

    fields: tuple[Field, ...]
    parts: Mapping[str, int] = field(default_factory=dict)


FORM_KEPT = timedelta(hours=1)
"""How long this process keeps a publication's form: a publication never
changes, so the time only bounds what is held."""


@dataclass(frozen=True, slots=True)
class RunsHeld:
    """A task's latest publication as its entrants' results read it:
    its runs in the task's order, each report's
    column as its plan gives it, each output's title and unit, the columns
    of each axis, the steps whose compile error `compiled` reads
    (`run_results.compilers`), the values that read a column the
    entrant could set, as its save checked them (#504), the task's
    settings, the inputs it gives that the entrant does not, but for its
    `secrets`, which are never shown, each workflow input's type, so
    `show` tells a file or folder as one, and every output of the workflow.
    """

    runs: tuple[Run, ...]
    columns: Mapping[str, Column] = field(default_factory=dict)
    compiling: frozenset[str] = frozenset()
    self_reported: frozenset[str] = frozenset()
    titles: Mapping[str, tuple[str | None, ...]] = field(default_factory=dict)
    axes: tuple[tuple[str, ...], ...] = ()
    meanings: Mapping[str, tuple[str | None, str | None]] = field(default_factory=dict)
    settings: Mapping[str, object] = field(default_factory=dict)
    kinds: Mapping[str, Type] = field(default_factory=dict)
    secrets: frozenset[str] = frozenset()
    outputs: tuple[str, ...] = ()


async def runs_held(ctx: Context, published: PublishedTask) -> RunsHeld:
    """The runs and plan of a task's latest publication, read
    once, since a publication never changes. A publication's runs file and
    plan always read, as its save wrote them, so one that does not fails
    loudly.
    """
    version = published.publication.version

    async def read() -> RunsHeld:
        found = await ctx.forge.content.read_file(PLATFORM, published.id, RUNS_PATH, at=version)
        planned = await ctx.forge.content.read_file(PLATFORM, published.id, PLAN_PATH, at=version)
        plan = RunPlan.from_bytes(planned.content)
        return held(
            published.definition,
            RunsFile.from_bytes(found.content),
            plan,
            await workflow_of(ctx, plan),
        )

    key = f"published.runs.{published.id}.{version}"
    return await ctx.memo.remembered(key, FORM_KEPT, read)


def held(
    definition: TaskDefinition, runs: RunsFile, plan: RunPlan, workflow: RunWorkflow
) -> RunsHeld:
    """A task's runs as its results read them, from its definition, runs
    file, plan and the workflow the plan pinned."""
    given = {name: value for name, value in definition.inputs.items() if name not in plan.entrant}
    secrets = frozenset(
        name for name, value in given.items() if isinstance(value, Mapping) and "secret" in value
    )
    return RunsHeld(
        runs.runs,
        {name: entry.column() for name, entry in plan.reports.items()},
        compilers(plan),
        runs.self_reported_values,
        {run.id: run.titles for run in plan.runs if run.titles is not None},
        tuple(tuple(axis.inputs) for axis in definition.repeat),
        {
            name: (output.title, output.unit)
            for name, output in workflow.outputs.items()
            if isinstance(output, RunOutput)
        },
        {name: value for name, value in given.items() if name not in secrets},
        {name: each.type for name, each in workflow.inputs.items()},
        secrets,
        tuple(workflow.outputs),
    )


async def workflow_of(ctx: Context, plan: RunPlan) -> RunWorkflow:
    """The workflow the plan pinned, whose outputs' titles and units and
    inputs' types a result reads. A publication's workflow reads, as its
    save wrote it.
    """
    found = await ctx.forge.workflows.read_workflow_at(
        PLATFORM,
        WorkflowId(plan.workflow.id),
        VersionId(plan.workflow.commit),
        WORKFLOW_FILE,
    )
    return parse_run_workflow(found.content)


async def plan_at(ctx: Context, task: TaskId, version: VersionId) -> RunPlan:
    """The plan of a task's publication at `version`, read once, since
    a publication never changes."""

    async def read() -> RunPlan:
        found = await ctx.forge.content.read_file(PLATFORM, task, PLAN_PATH, at=version)
        return RunPlan.from_bytes(found.content)

    return await ctx.memo.remembered(f"published.plan_at.{task}.{version}", FORM_KEPT, read)


async def form(ctx: Context, published: PublishedTask) -> TaskForm:
    """The task's form at its latest publication, read from its plan."""

    async def read() -> TaskForm:
        found = await ctx.forge.content.read_file(
            PLATFORM, published.id, PLAN_PATH, at=published.publication.version
        )
        planned = RunPlan.from_bytes(found.content)
        parts = {name: each.parts for name, each in planned.entrant.items() if each.parts}
        return TaskForm(run_fields_of(planned.entrant, published.definition.inputs), parts)

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


async def results_released(ctx: Context, task: TaskId) -> bool:
    """Whether the task's results are released now: its latest action a
    release, never when it has none. Read afresh on every call and never
    kept, so a take-back hides `after_release` from every reader at once.
    """
    return await released_since(ctx, task) is not None


async def released_since(ctx: Context, task: TaskId) -> datetime | None:
    """When the release that stands was made, by the package's clock, none
    when the task's results are not released: its latest action, in the
    order the rows were written (`releases.seq`), is not a release.
    """
    latest = (
        await ctx.db.execute(
            select(Release.action, Release.acted_at)
            .where(Release.task_id == task)
            .order_by(Release.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    return latest.acted_at if latest is not None and latest.action == RELEASED else None


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


def shown_now(definition: TaskDefinition, path: str) -> bool:
    """Whether the task shows the file at `path` with its statement: one
    under `public/`, which needs no `show` entry, unless only a check names
    it (`checks.check_only`).
    """
    return path.startswith(PUBLIC_FOLDER) and not check_only(definition, path)


async def public_file(ctx: Context, published: PublishedTask, path: str) -> PublicFile:
    """The file at `path` as the task's latest publication holds it, when
    the task shows it with its statement (`shown_now`), read as the
    platform, its large-file object's bytes for an upload. The caller has
    checked the reader may read the task's statement and the path with
    `public_path`. `NotFound` for a file not shown or not there, and
    `PublicFileTooLarge` for one over `PUBLIC_MOST` bytes, read no further.
    """
    if not shown_now(published.definition, path):
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
    published, for whoever organises the task: one under `public/`. The
    caller has checked who asks. `NotFound` for a task or a file not there,
    or one the task never shows.
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
        return False
    return shown_now(definition, path)


@dataclass(frozen=True, slots=True)
class ShownInput:
    """An input `show` shows that is a file or a folder, as its reader
    fetches it: the door to a file's bytes (`Door`), served whole whatever
    its size, with the file's `name`, the last part of its path, to save it
    under; or a folder's files, by their paths below it, each fetched by its
    path.
    """

    door: Door | None = None
    name: str | None = None
    files: tuple[str, ...] = ()


async def task_inputs(
    ctx: Context, published: PublishedTask, *, revealed: bool
) -> run_showing.TaskInputs:
    """What the task's page shows of its inputs now, its shared inputs and
    the runs whose inputs are shown (`run_showing.task_inputs`), for a
    reader who may read its statement: `revealed` is whether the task has
    closed for every row, and a release is read afresh, so a take-back
    hides at once.
    """
    held = await runs_held(ctx, published)
    return run_showing.task_inputs(
        published.definition.scoring,
        held.runs,
        revealed=revealed,
        released=await results_released(ctx, published.id),
        titles=held.titles,
        axes=held.axes,
        settings=held.settings,
        kinds=held.kinds,
    )


async def shown_input(
    ctx: Context,
    published: PublishedTask,
    shown: tuple[Type, str] | None,
    path: str | None,
    *,
    listing: bool = False,
) -> ShownInput:
    """The input an answer shows as `shown`, a file or a folder by its path
    in the task (`run_showing.shown_file`), as the task's latest
    publication holds it: a file's door; a folder's files, or with `path`
    the door to the one at that path below it. A `listing` opens no door,
    so a file answers only its name. `NotFound` for an input not shown,
    which tells nothing of one hidden, and for a path the folder does not
    hold.
    """
    if shown is None:
        raise NotFound(NO_SUCH_INPUT)
    kind, at = shown
    task, version = published.id, published.publication.version
    if kind is Type.FILE:
        if path is not None:
            raise NotFound(NO_SUCH_FILE)
        name = at.rsplit("/", 1)[-1]
        if listing:
            return ShownInput(name=name)
        door = await ctx.forge.content.media_door(PLATFORM, task, at, at=version)
        return ShownInput(door=door, name=name)
    folder = at if at.endswith("/") else f"{at}/"
    held = await ctx.forge.content.list_files(PLATFORM, task, at=version)
    files = tuple(sorted(each[len(folder) :] for each in held.tokens if each.startswith(folder)))
    if path is None:
        return ShownInput(files=files)
    if path not in files:
        raise NotFound(NO_SUCH_FILE)
    if listing:
        return ShownInput(name=path.rsplit("/", 1)[-1])
    door = await ctx.forge.content.media_door(PLATFORM, task, f"{folder}{path}", at=version)
    return ShownInput(door=door, name=path.rsplit("/", 1)[-1])


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
