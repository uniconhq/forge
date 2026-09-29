"""Whether the signed-in person sees a task and may submit to it, worked out
on every read from the settings and the clock by the rules in
`forge.domain.release`. The contest's settings are its `contest.yaml` now;
the task's are the `task.yaml` its latest publication froze, since that is
the task contestants get; the person's own time extension is on their
`contestants` row, and without one they have none. Everything is read as the
platform, since a contestant can read neither, so the contest's own
`visibility` is applied first: a person the contest is hidden from is told
there is no such task. A task with no publication is not released. Nothing at
the forge changes when a task becomes released.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.db.tables.contestants import APPROVED
from forge.domain import release as rules
from forge.domain.definitions import CONTEST_FILE, TASK_FILE, parse_contest, parse_task
from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.release import Closed
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import roles


@dataclass(frozen=True, slots=True)
class TaskRelease:
    """Whether the task is released, visible and open to one person now, and
    when it is not open, the first reason why.
    """

    released: bool
    visible: bool
    open: bool
    closed: Closed | None


@action
async def of_task(ctx: Context, session: Session, task: TaskId) -> TaskRelease:
    """Whether the task is released, visible and open to the signed-in person
    now, by the server's clock. `NotFound` when the contest is hidden from
    them.
    """
    contest_id = contest_id_of(task_scope(task))
    contest = parse_contest(
        (await ctx.forge.content.read_file(PLATFORM, contest_id, CONTEST_FILE)).content
    )
    row = await _contestant(ctx, contest_id, session.user_id)
    if not rules.contest_visible_to(
        contest,
        has_session=True,
        is_contestant=row is not None and row.status == APPROVED,
        is_organiser=await roles.holds_role_in_contest(ctx, session.user_id, contest_id),
    ):
        raise NotFound("There is no such task.")
    published = await ctx.forge.workspaces.list_publications(task)
    if not published:
        return TaskRelease(released=False, visible=False, open=False, closed=Closed.NOT_RELEASED)
    definition = parse_task(
        (
            await ctx.forge.content.read_file(PLATFORM, task, TASK_FILE, at=published[-1].version)
        ).content
    )
    now = ctx.now
    extension = timedelta(seconds=row.time_extension_seconds if row is not None else 0)
    openness = rules.openness(contest, definition, now, extension)
    return TaskRelease(
        released=rules.released(contest, definition, now),
        visible=rules.visible(contest, definition, now),
        open=openness.open,
        closed=openness.reason,
    )


async def _contestant(ctx: Context, contest: ContestId, user_id: int) -> Contestant | None:
    return (
        await ctx.db.execute(
            select(Contestant).where(
                Contestant.contest_id == contest, Contestant.user_id == user_id
            )
        )
    ).scalar_one_or_none()
