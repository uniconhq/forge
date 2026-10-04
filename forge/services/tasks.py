"""Making a task, listing a contest's tasks, and where its files stand
against its publications. A task is one place at the forge holding
`task.yaml`, `statement.md` and an example testcase in `data/testcases/`,
with three roles of its own. `create` makes it before it answers: the place
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
`contest.yaml` orders, labels and scores them. A new task is added to the
end of that list by the platform, with the next free letter as its label and
100 points (`forge.domain.contest_entries`), so it shows on the contest's
pages without anyone editing the file first. The edit is made in the file's
text, so the organisers' comments and layout stay. When the file does not
read as a `contest.yaml`, or its list cannot be added to line by line, the
task is made without an entry and an organiser adds one by hand; an
organiser's change to the file between the read and the write fails the
request like any other step, and a task already in the list is left as it
is.

A task's name is reserved when it is asked for and the task is made at the
forge under its key; the name is what `contest.yaml` lists it by.
"""

from dataclasses import dataclass

from forge.domain.contest_entries import Unlisted, with_entry
from forge.domain.definitions import CONTEST_FILE, starter_task, title_of
from forge.domain.errors import Conflict, Forbidden, NotFound, PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, OrgId, TaskId, VersionId
from forge.domain.names import Named, validate_contest_or_task_name
from forge.domain.publications import Publication
from forge.domain.roles import Role, contest_id_of, contest_scope, task_scope
from forge.domain.yaml_models import InvalidDefinition, Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import making, names, org_accounts, publications
from forge.services.access import Organiser, require

log = get_logger(__name__)

LIST_TRIES = 3


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
async def list(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[Named, ...]:
    """Every task in the contest with its name, by name, read as the
    organiser, so they see the ones the forge lets them read. Needs the
    observer role at the contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    return await names.named(ctx, await ctx.forge.content.list_tasks(organiser.identity, contest))
