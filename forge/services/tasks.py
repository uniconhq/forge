"""Making a task, listing a contest's tasks, following how far making one has
got, and where its files stand against its publications. A task is one place
at the forge holding `task.yaml`, `statement.md` and placeholder files that
keep `data/testcases/` and `checker/` there, with three roles of its own.
`create` writes the request and answers at once; the `provisioning` poller
runs `provision`: the place with its starter files, then its roles and
protection, which `content.secure` attaches and checks, reserving the task's
publications for the platform in the same call. Nothing is published until
the first save.

A contest's tasks are the tasks there are at the forge. The `tasks` list in
`contest.yaml` orders, labels and scores them, and creating a task does not
touch it.
"""

from dataclasses import dataclass

from forge.db.tables import Provisioning
from forge.domain.definitions import starter_task, title_of
from forge.domain.errors import Conflict, NotFound
from forge.domain.ids import ContestId, TaskId, VersionId
from forge.domain.names import validate_contest_or_task_name
from forge.domain.publications import Publication
from forge.domain.roles import Role, Scope, contest_id_of, contest_scope, task_id_of, task_scope
from forge.domain.yaml_models import Problem
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import provisioning, publications
from forge.services.access import Organiser, require
from forge.services.provisioning import Attempt, Record

log = get_logger(__name__)

KIND = "task"


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
) -> Record:
    """Ask for the task to be made in the contest, titled `title` or, without
    one, its name. Needs the manager role at the contest. `InvalidName` for a
    name that breaks the rules; `NotFound` when the contest is not there;
    `Conflict` when the name is taken or being made.
    """
    parent = contest_scope(contest)
    require(organiser, parent, Role.MANAGER)
    validate_contest_or_task_name(name)
    if not await ctx.forge.content.exists(contest):
        raise NotFound(f"There is no contest {parent.name}.")
    task = task_id_of(Scope(parent.org, parent.contest, name))
    row = await provisioning.request(
        ctx, KIND, task, {"title": title_of(title, name), "creator_user_id": organiser.user.id}
    )
    log.info("tasks.requested", task=task, user_id=organiser.user.id)
    return provisioning.record_from(row)


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over a task's row: the steps, in order, each safe to
    run again. A task the first try made before its record caught up is
    taken as made.
    """
    task = TaskId(row.target_id)
    scope = task_scope(task)
    name = str(scope.task)
    title = str(row.payload.get("title") or name)

    async def make_repo(attempt: Attempt) -> None:
        try:
            await ctx.forge.content.create_task(contest_id_of(scope), name, starter_task(title))
        except Conflict:
            if not attempt.is_rerun:
                raise

    async def make_roles(attempt: Attempt) -> None:
        await ctx.forge.content.secure(task)

    await provisioning.run(
        ctx, row, provisioning.steps(KIND, {"repo": make_repo, "roles": make_roles})
    )


@action
async def status(ctx: Context, organiser: Organiser, task: TaskId) -> Record | None:
    """Where making the task has got to, for anyone observing its contest, or
    none when nothing has asked for it.
    """
    scope = task_scope(task)
    require(organiser, Scope(scope.org, scope.contest), Role.OBSERVER)
    return await provisioning.record_of(ctx, KIND, task)


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
async def list(ctx: Context, organiser: Organiser, contest: ContestId) -> tuple[TaskId, ...]:
    """Every task in the contest, by name, read as the organiser, so they see
    the ones the forge lets them read. Needs the observer role at the
    contest.
    """
    require(organiser, contest_scope(contest), Role.OBSERVER)
    return await ctx.forge.content.list_tasks(organiser.identity, contest)
