"""Making a task, listing a contest's tasks, and where its files stand
against its publications. A task is one place at the forge holding
`task.yaml`, `statement.md`, an empty `public/` and one test in
`tests/main/1/`, with three roles of its own. `create` makes it before it answers: the place
with its starter files; its roles and protection, which `content.secure`
attaches, reserving the task's publications for the platform in the same
call; its activation at the CI, as the org's own account, which takes the
task for grading, trusts it for `volumes` and nothing else, and forgets the
event push the activation leaves behind, since the platform starts every
run itself; and the task's entry in `contest.yaml`. A step that fails fails
the request, which writes nothing here, and what the try already made is
removed again, the latest first, before the person is told: the entry, when
`contest.yaml` still reads as the try left it, the activation, and the task
with its roles. That is best effort; what a removal could not remove is
logged (`forge.services.making`). The person asks again. Nothing is
published until the first save.

A contest's tasks are the tasks there are at the forge. The `tasks` list in
`contest.yaml` orders them, a task's label being its place in it, and gives
each its timeline and its worth. A new task is added to the end of that list
by the platform as `{id: <name>}`, and in a published contest with its
`release_at` and `closes` at the contest's end, so work in progress shows
nothing until the organisers move them (`forge.domain.contest_entries`).
The edit is made in the file's text, so the organisers' comments and layout
stay. When the file does not
read as a `contest.yaml`, or its list cannot be added to line by line, the
task is made without an entry and an organiser adds one by hand; an
organiser's change to the file between the read and the write fails the
request like any other step, and a task already in the list is left as it
is.

A task's name is reserved when it is asked for and the task is made at the
forge under its key; the name is what `contest.yaml` lists it by.

`standing` is the list an organiser works a contest's tasks from: each task
the contest lists, in its order and under the letter of its place, with
where its files stand (`state`) and its timeline from its entry, each time
at its default where the entry gives none. A task of the org the contest
does not list is not on it.
"""

import builtins
from dataclasses import dataclass
from datetime import datetime, timedelta

from forge.domain.contest_entries import Unlisted, with_entry
from forge.domain.definitions import (
    CONTEST_FILE,
    DEFAULT_WORTH,
    TASK_FILE,
    ContestDefinition,
    ContestTask,
    letters,
    parse_task,
    starter_task,
    title_of,
)
from forge.domain.errors import Conflict, Forbidden, NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId
from forge.domain.names import Named, validate_contest_or_task_name
from forge.domain.publications import Publication
from forge.domain.release import NONE, due_of
from forge.domain.roles import Role, contest_id_of, contest_scope, task_scope
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import making, names, org_accounts, publications, published
from forge.services.access import Organiser, require

log = get_logger(__name__)

LIST_TRIES = 3
POINTS_KEPT = timedelta(hours=1)
"""How long this process keeps whether a publication gives points: a
publication never changes, so the time only bounds what is held."""


@dataclass(frozen=True, slots=True)
class TaskState:
    """Where a task's files stand: the version at their head, the latest
    publication, and whether the head is a draft, a state no publication
    froze. A draft carries the errors that keep it from being published, each
    at its YAML path; a draft with none was held back, or not saved since it
    was made.
    """

    head: VersionId
    latest: Publication | None
    draft: bool
    errors: tuple[Problem, ...]


@dataclass(frozen=True, slots=True)
class Timeline:
    """A task's timeline in its contest, from its entry in `contest.yaml`,
    each time at its default where the entry gives none: `worth`, the most
    points it gives, 100 unless the entry says, and none on a task whose
    latest publication gives no points or that has none; `release_at`, the
    contest's start unless the entry says; `due`, after which a submission
    is late, and `late_per_day`, the fraction a started late day takes off,
    1 unless the entry says, both none on a task with no due; and `closes`,
    the contest's end unless the entry says.
    """

    worth: int | float | None
    release_at: datetime
    due: datetime | None
    late_per_day: int | float | None
    closes: datetime


@dataclass(frozen=True, slots=True)
class TaskStanding:
    """One task of a contest as its organisers work from it: the task with
    its name, its letter by its place in the contest's `tasks`, where its
    files stand against its latest publication, and its timeline.
    """

    task: Named
    label: str
    state: TaskState
    timeline: Timeline


@action
async def create(
    ctx: Context, organiser: Organiser, contest: ContestId, name: str, *, title: str | None = None
) -> Named:
    """Make the task in the contest, titled `title` or, without one, its
    name. Needs the manager role at the contest. `InvalidName` for a name
    that breaks the rules; `NotFound` when the contest is not there;
    `Conflict` when the name is taken.
    """
    parent = contest_scope(contest)
    require(organiser, parent, Role.MANAGER)
    validate_contest_or_task_name(name)
    if not await ctx.forge.content.exists(contest):
        raise NotFound(f"There is no contest {(await names.labelled(ctx, parent)).name}.")
    task = await names.reserve_task(ctx, contest, name)
    made = making.undo_on_rollback(ctx, "tasks", task=task)
    try:
        await making.place(
            made,
            "task",
            task,
            ctx.forge.content.create_task(
                contest, str(task_scope(task).task), starter_task(title_of(title, name))
            ),
            lambda: ctx.forge.content.delete_place(task),
        )
        await ctx.forge.content.secure(task)
        account = await org_accounts.identity(ctx, OrgId(parent.org))
        made.add("activation", task, lambda: ctx.forge.grading.deactivate(account, task))
        try:
            await ctx.forge.grading.activate(account, task)
        except Forbidden:
            account = await org_accounts.renew(ctx, account)
            await ctx.forge.grading.activate(account, task)
        await _list_in_contest(ctx, task, name, made)
    except PortError as exc:
        raise making.failure(exc, "tasks.step_failed", task=task) from None
    log.info("tasks.created", task=task, user_id=organiser.user.id)
    return Named(task, name)


async def _list_in_contest(ctx: Context, task: TaskId, name: str, made: making.Made) -> None:
    """Add the task to the end of its contest's `tasks` list, as the
    platform, unless it is there already or the list cannot be added to.
    The entry is noted in `made`, and taken out again only while the file
    still says exactly what this wrote, so an organiser's change since is
    never undone. Two tasks made in one contest at once both write the
    file, so the one that loses the race reads it again and adds itself to
    what the other wrote.
    """
    contest = contest_id_of(task_scope(task))
    for tried in range(1, LIST_TRIES + 1):
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        try:
            updated = with_entry(found.content, name)
        except (InvalidDefinition, Unlisted) as exc:
            log.warning("tasks.not_listed", task=task, reason=str(exc))
            return
        if updated is None:
            return
        try:
            await ctx.forge.content.write_file(
                PLATFORM,
                contest,
                CONTEST_FILE,
                updated,
                message=f"Add {name} to the contest's tasks",
                expected=found.token,
            )
        except Conflict:
            if tried == LIST_TRIES:
                raise
            continue
        break

    async def unlist() -> None:
        now = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
        if now.content != updated:
            raise Conflict(f"{CONTEST_FILE} changed after the task was added")
        await ctx.forge.content.write_file(
            PLATFORM,
            contest,
            CONTEST_FILE,
            found.content,
            message=f"Take {name} out of the contest's tasks",
            expected=now.token,
        )

    made.add("contest_entry", contest, unlist)
    log.info("tasks.listed", task=task)


@action
async def state(ctx: Context, organiser: Organiser, task: TaskId) -> TaskState:
    """Where the task's files stand against its latest publication. When the
    head is not what that publication froze, the head is checked the way a
    save checks it, as the organiser, so the errors are always the current
    ones. Needs the observer role at the task.
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    head = await ctx.forge.content.list_files(organiser.identity, task)
    published = await ctx.forge.workspaces.list_publications(task)
    latest = published[-1] if published else None
    if latest is not None and latest.version == head.version:
        return TaskState(head=head.version, latest=latest, draft=False, errors=())
    checked = await publications.check(ctx, organiser.identity, task, head, {})
    return TaskState(head=head.version, latest=latest, draft=True, errors=checked.errors)


@action
async def standing(
    ctx: Context, organiser: Organiser, contest: ContestId
) -> tuple[TaskStanding, ...]:
    """Every task the contest lists, in the order of its `tasks`, each with
    its letter, where its files stand, as `state` gives it, and its timeline.
    Needs the observer role at the contest. `NotFound` when the contest's
    settings do not read.

    Each task costs the forge what `state` does, its head and its
    publications, a check of its head when that is a draft, and the
    `task.yaml` of its latest publication once per process, for whether it
    gives points.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await published.contest(ctx, contest)
    ids = await names.task_ids(ctx, contest, [entry.id for entry in settings.tasks])
    await ctx.let_go()
    found: builtins.list[TaskStanding] = []
    for index, entry in enumerate(settings.tasks):
        task = ids.get(entry.id)
        if task is None:
            continue
        where = await state(ctx, organiser, task)
        found.append(
            TaskStanding(
                task=Named(task, entry.id),
                label=letters(index),
                state=where,
                timeline=await _timeline(ctx, settings, entry, task, where.latest),
            )
        )
    return tuple(found)


async def _timeline(
    ctx: Context,
    settings: ContestDefinition,
    entry: ContestTask,
    task: TaskId,
    latest: Publication | None,
) -> Timeline:
    gives_points = latest is not None and await _gives_points(ctx, task, latest)
    return Timeline(
        worth=(entry.worth if entry.worth is not None else DEFAULT_WORTH) if gives_points else None,
        release_at=settings.release_of(entry),
        due=due_of(settings, entry, NONE),
        late_per_day=settings.late_per_day_of(entry),
        closes=settings.closes_of(entry),
    )


async def _gives_points(ctx: Context, task: TaskId, latest: Publication) -> bool:
    """Whether the task as `latest` froze it gives points, read once per
    process for each publication.
    """

    async def read() -> bool:
        found = await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=latest.version)
        try:
            return parse_task(found.content).gives_points
        except InvalidDefinition:
            return False

    return await ctx.memo.remembered(f"tasks.gives_points.{latest.id}", POINTS_KEPT, read)


@action
async def list(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Named, ...]:
    """Every task in the contest with its name, by name, read as the
    organiser, so they see the ones the forge lets them read. Needs the
    observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    return await names.named(ctx, await ctx.forge.content.list_tasks(organiser.identity, contest))
