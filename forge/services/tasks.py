"""Making a task, listing a contest's tasks, and where its files stand
against its publications. A task is one place at the forge holding
`task.yaml`, `statement.md` and one test, `tests/1.in` and `tests/1.ans`,
with three roles of its own. `create` makes it before it answers: the place
with its starter files; its roles and protection, which `content.secure`
attaches, reserving the task's publications for the platform in the same
call; and the task's entry in `contest.yaml`. Its gradings run on the org's
dispatch place, so the CI is not told of the task. A step that fails fails
the request, which writes nothing here, and what the try already made is
removed again, the latest first, before the person is told: the entry, when
`contest.yaml` still reads as the try left it, and the task with its
roles. That is best effort; what a removal could not remove is
logged (`forge.services.making`). The person asks again. Nothing is
published until the first save.

A contest's tasks are the tasks there are at the forge. The `tasks` list in
`contest.yaml` orders them, a task's label being its place in it, and gives
each its timeline. A new task is added to the end of that list
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
at its default where the entry gives none, and whether it is withdrawn,
its submissions and what taking it out would do (`taking_out`). A
withdrawn task keeps its place and its letter. A task of the org the
contest does not list is not on it.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from forge.domain.contest_entries import Unlisted, with_entry
from forge.domain.definitions import (
    CONTEST_FILE,
    STATEMENT_FILE,
    ContestDefinition,
    ContestTask,
    letters,
    parse_contest,
    starter_task,
    title_of,
)
from forge.domain.errors import Conflict, NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.names import Named, validate_contest_or_task_name
from forge.domain.publication_record import Publication
from forge.domain.release import NONE, due_of
from forge.domain.roles import Role, contest_id_of, contest_scope, task_scope
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import (
    entrant_view,
    making,
    names,
    pipelines,
    results,
    taking_out,
    task_save,
)
from forge.services.access import Organiser, require
from forge.services.results import ResultsState
from forge.services.taking_out import TakeOut

log = get_logger(__name__)

LIST_TRIES = 3


@dataclass(frozen=True, slots=True)
class TaskState:
    """Where a task's files stand: the version at their head, the latest
    publication, and whether the head is a draft, a state no publication
    froze. A draft carries the errors that keep it from being published, each
    at its YAML path; a draft with none was held back, or not saved since it
    was made. `statement_written` says whether the latest publication holds
    a statement, `statement.md` there, not blank and not the starter's
    placeholder (false when nothing is published), which the contest's
    opening checklist asks of each task (M57); `head_statement_written`
    says the same of the head. `results` says whether the task's results
    are released now, with every release and take-back, who and when.
    """

    head: VersionId
    latest: Publication | None
    draft: bool
    errors: tuple[Problem, ...]
    statement_written: bool
    head_statement_written: bool
    results: ResultsState


@dataclass(frozen=True, slots=True)
class Timeline:
    """A task's timeline in its contest, from its entry in `contest.yaml`,
    each time at its default where the entry gives none: `release_at`, the
    contest's start unless the entry says; `due`, after which a submission
    is late, none on a task with no due; and `closes`, the contest's end
    unless the entry says.
    """

    release_at: datetime
    due: datetime | None
    closes: datetime


@dataclass(frozen=True, slots=True)
class TaskStanding:
    """One task of a contest as its organisers work from it: the task with
    its name, its letter by its place in the contest's `tasks`, where its
    files stand against its latest publication, and its timeline; whether
    it is withdrawn, how many submissions it has, any entrant's, and what
    taking it out of the contest would do (`taking_out`), none while it is
    withdrawn, when it can only be put back.
    """

    task: Named
    label: str
    state: TaskState
    timeline: Timeline
    withdrawn: bool = False
    submissions: int = 0
    take_out: TakeOut | None = None


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
        await _list_in_contest(ctx, task, name, made, organiser.user.id)
    except PortError as exc:
        raise making.failure(exc, "tasks.step_failed", task=task) from None
    log.info("tasks.created", task=task, user_id=organiser.user.id)
    return Named(task, name)


async def _list_in_contest(
    ctx: Context, task: TaskId, name: str, made: making.Made, author: int
) -> None:
    """Add the task to the end of its contest's `tasks` list, as the
    platform with `author` named as the change's author, unless it is
    there already or the list cannot be added to.
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
                author=author,
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
            author=author,
        )

    made.add("contest_entry", contest, unlist)
    log.info("tasks.listed", task=task)


@action
async def state(ctx: Context, organiser: Organiser, task: TaskId) -> TaskState:
    """Where the task's files stand against its latest publication. When the
    head is not what that publication froze, the head is checked the way a
    save checks it, as the organiser, so the errors are always the current
    ones. A run task's latest publication that has not taken over says
    where it stands (`pipelines.checks`). Needs the observer role at the
    task. Where its results stand is read afresh (`results.state_of`).
    """
    require(organiser, task_scope(task), Role.OBSERVER)
    head = await ctx.forge.content.list_files(organiser.identity, task)
    made = await ctx.forge.workspaces.list_publications(task)
    latest = made[-1] if made else None
    if latest is not None:
        check = (await pipelines.checks(ctx, task, made)).get(latest.id)
        failed = await pipelines.failed(ctx, task, [latest.id] if check == "refused" else [])
        latest = replace(latest, check=check, failed_checks=failed.get(latest.id, ()))
    token = head.tokens.get(STATEMENT_FILE)
    written = token is not None and await entrant_view.statement_written_at(
        ctx, task, head.version, token
    )
    results_now = await results.state_of(ctx, task)
    if latest is not None and latest.version == head.version:
        return TaskState(head.version, latest, False, (), written, written, results_now)
    checked = await task_save.check(ctx, organiser.identity, task, head, {})
    published_written = latest is not None and await entrant_view.statement_written_at(
        ctx, task, latest.version, latest.id
    )
    return TaskState(
        head.version, latest, True, checked.errors, published_written, written, results_now
    )


@action
async def standing(
    ctx: Context, organiser: Organiser, contest: ContestId
) -> tuple[TaskStanding, ...]:
    """Every task the contest lists, in the order of its `tasks`, each with
    its letter, where its files stand, as `state` gives it, and its timeline.
    Needs the observer role at the contest. `InvalidDefinition` naming
    `contest.yaml`, with every problem, when its settings do not read, as a
    save of them would be refused, and `NotFound` for no such contest.

    Each task costs the forge what `state` does, its head and its
    publications and a check of its head when that is a draft.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await _settings(ctx, contest)
    ids = await names.task_ids(ctx, contest, [entry.id for entry in settings.tasks])
    counts = await taking_out.submission_counts(ctx, [*ids.values()])
    await ctx.let_go()
    found: list[TaskStanding] = []
    for index, entry in enumerate(settings.tasks):
        task = ids.get(entry.id)
        if task is None:
            continue
        where = await state(ctx, organiser, task)
        count = counts.get(task, 0)
        found.append(
            TaskStanding(
                task=Named(task, entry.id),
                label=letters(index),
                state=where,
                timeline=_timeline(settings, entry),
                withdrawn=entry.withdrawn,
                submissions=count,
                take_out=None if entry.withdrawn else taking_out.take_out_of(count),
            )
        )
    return tuple(found)


async def _settings(ctx: Context, contest: ContestId) -> ContestDefinition:
    """The contest's settings as they stand, read as the platform, for its
    organisers, who mend them: `InvalidDefinition` naming `contest.yaml`
    with every problem when they do not read, and `NotFound` for no such
    contest.
    """
    try:
        found = await ctx.forge.content.read_file(PLATFORM, contest, CONTEST_FILE)
    except NotFound as exc:
        raise NotFound(names.NO_SUCH_CONTEST) from exc
    return parse_contest(found.content)


def _timeline(settings: ContestDefinition, entry: ContestTask) -> Timeline:
    return Timeline(
        release_at=settings.release_of(entry),
        due=due_of(settings, entry, NONE),
        closes=settings.closes_of(entry),
    )


@action
async def list_tasks(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Named, ...]:
    """Every task in the contest with its name, by name, read as the
    organiser, so they see the ones the forge lets them read. Needs the
    observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    return await names.named(ctx, await ctx.forge.content.list_tasks(organiser.identity, contest))
