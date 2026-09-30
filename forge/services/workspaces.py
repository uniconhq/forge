"""Opening an approved contestant's workspace at the forge, and giving it a
place to submit every task that is published. A workspace is where a
contestant works for one contest: a desk their questions live in, and one
place per task to submit to, with the contestant able to write each.

Each part is a thing of its own in `provisioning`, so a part that fails is
tried again alone and nothing is made twice. Approval asks for the
`workspace` row, which the poller runs in two steps: the desk, which names
the workspace on the contestant's row, and then a `submission_place` row
asked for each task that has a publication by then, released or not. A task
first published after that is found by the save that publishes it, which
asks for a `submission_place` row for every approved contestant who has none
for the task. The poller makes each place from its row.

The two never leave a gap between them. A save publishes at the forge before
it looks for the contestants, and a workspace lists the tasks only after its
contestant's approval has landed, so either the save sees the approved
contestant or the workspace sees the publication. A place asked for before
its contestant's desk is open fails and is tried again, so a place is only
ever made in a workspace the contestant's row names.

Every part reads the contestant's row first and holds it until the tick
ends, and does nothing for anyone no longer approved. Removing a contestant
waits on that row, so a removal and a part never pass each other: the part
either sees the removal, or finishes before the removal takes the access
away again. Removal takes the access away from the workspace the row names,
or, when a try opened the desk and the tick that would have named it never
landed, from the workspace the contestant's name gives. The workspace is
ready once its desk and every place asked for it are made.
"""

import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select

from forge.db.tables import Contestant, Provisioning
from forge.domain.errors import Conflict, NotFound, ServiceError
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import UserOwner
from forge.domain.provisioning import STEPS
from forge.domain.registration import Status, WorkspaceState
from forge.domain.roles import contest_id_of, task_scope
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import provisioning, published
from forge.services.provisioning import Attempt

log = get_logger(__name__)

KIND = "workspace"
PLACE_KIND = "submission_place"
DESK, PLACES = STEPS[KIND]
(PLACE,) = STEPS[PLACE_KIND]
SEPARATOR = "/"


class DeskNotOpen(ServiceError):
    """A place to submit asked for before the contestant's desk is open,
    which the poller tries again.
    """

    code = "desk_not_open"


@dataclass(frozen=True, slots=True)
class Readiness:
    """Where one contestant's workspace stands, and while a part of it is
    failing, the reason, in the platform's words.
    """

    workspace: WorkspaceState
    error: str | None


async def request(ctx: Context, contestant: Contestant) -> None:
    """Ask for the approved contestant's workspace to be made."""
    with contextlib.suppress(Conflict):
        await provisioning.request(ctx, KIND, str(contestant.id), {})
        log.info("workspaces.requested", contest=contestant.contest_id, user_id=contestant.user_id)


async def close(ctx: Context, contestant: Contestant) -> None:
    """Take the contestant's access to their workspace away and keep what is
    in it. Without a workspace on the row, the one their name gives is closed
    all the same, since a try may have opened its desk and never landed; a
    contestant whose account is gone has nothing left to take away.
    """
    workspace = contestant.workspace_id
    if workspace is None:
        try:
            user = await ctx.forge.identity.find_user(contestant.user_id)
        except NotFound:
            return
        workspace = ctx.forge.workspaces.workspace_of(
            ContestId(contestant.contest_id), UserOwner(user.username)
        )
    await ctx.forge.workspaces.close_workspace(WorkspaceId(workspace), [contestant.user_id])
    log.info("workspaces.closed", contest=contestant.contest_id, user_id=contestant.user_id)


async def place_for_everyone(ctx: Context, task: TaskId) -> int:
    """Ask for a place to submit the task for every approved contestant of its
    contest who has none, and return how many were asked for. Run after the
    task is published, every time, and by the nightly pass, so a place a lost
    unit of work never asked for is asked for again.
    """
    approved = (
        await ctx.db.execute(
            select(Contestant.id).where(
                Contestant.contest_id == contest_id_of(task_scope(task)),
                Contestant.status == Status.APPROVED,
            )
        )
    ).scalars()
    asked = await _ask_for_places(ctx, [_place_target(contestant, task) for contestant in approved])
    if asked:
        log.info("workspaces.places_requested", task=task, contestants=asked)
    return asked


async def readiness(ctx: Context, contestants: Sequence[Contestant]) -> dict[uuid.UUID, Readiness]:
    """Where the workspace of each approved contestant stands: ready once its
    desk and every place asked for it are made, and preparing until then.
    Contestants who are not approved have no workspace and are left out.
    """
    approved = {row.id for row in contestants if row.status == Status.APPROVED}
    if not approved:
        return {}
    targets = [str(contestant_id) for contestant_id in approved]
    rows = (
        await ctx.db.execute(
            select(Provisioning).where(
                or_(
                    and_(Provisioning.kind == KIND, Provisioning.target_id.in_(targets)),
                    and_(
                        Provisioning.kind == PLACE_KIND,
                        func.split_part(Provisioning.target_id, SEPARATOR, 1).in_(targets),
                    ),
                )
            )
        )
    ).scalars()
    parts: dict[uuid.UUID, list[Provisioning]] = {contestant_id: [] for contestant_id in approved}
    for row in rows:
        parts[_contestant_of(row)].append(row)
    return {contestant_id: _readiness(found) for contestant_id, found in parts.items()}


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over a workspace's row: the desk, then a place asked
    for every task published by now, each safe to run again.
    """
    contestant_id = uuid.UUID(row.target_id)

    async def make_desk(attempt: Attempt) -> None:
        contestant = await _approved(ctx, contestant_id)
        if contestant is None or contestant.workspace_id is not None:
            return
        user = await ctx.forge.identity.find_user(contestant.user_id)
        contestant.workspace_id = await ctx.forge.workspaces.open_workspace(
            ContestId(contestant.contest_id), UserOwner(user.username), [contestant.user_id]
        )
        await ctx.db.flush()
        log.info("workspaces.desk_opened", contest=contestant.contest_id, user_id=user.id)

    async def ask_for_places(attempt: Attempt) -> None:
        contestant = await _approved(ctx, contestant_id)
        if contestant is None:
            return
        contest = ContestId(contestant.contest_id)
        settings = await published.contest(ctx, contest)
        tasks = await published.tasks(ctx, contest, settings)
        await _ask_for_places(ctx, [_place_target(contestant.id, task.id) for task in tasks])

    await provisioning.run(
        ctx, row, provisioning.steps(KIND, {DESK: make_desk, PLACES: ask_for_places})
    )


async def provision_place(ctx: Context, row: Provisioning) -> None:
    """The poller's work over one place to submit one task."""
    contestant_id, task = _parse_place_target(row.target_id)

    async def make_place(attempt: Attempt) -> None:
        contestant = await _approved(ctx, contestant_id)
        if contestant is None:
            return
        if contestant.workspace_id is None:
            raise DeskNotOpen("The contestant's desk is not open yet.")
        await ctx.forge.workspaces.open_submission_place(
            WorkspaceId(contestant.workspace_id), task, [contestant.user_id]
        )
        log.info("workspaces.place_opened", task=task, user_id=contestant.user_id)

    await provisioning.run(ctx, row, provisioning.steps(PLACE_KIND, {PLACE: make_place}))


async def _ask_for_places(ctx: Context, targets: Sequence[str]) -> int:
    """Ask for each place that has no row yet, and return how many that was."""
    recorded = set(
        (
            await ctx.db.execute(
                select(Provisioning.target_id).where(
                    Provisioning.kind == PLACE_KIND, Provisioning.target_id.in_(targets)
                )
            )
        ).scalars()
    )
    asked = 0
    for target in sorted(set(targets) - recorded):
        with contextlib.suppress(Conflict):
            await provisioning.request(ctx, PLACE_KIND, target, {})
            asked += 1
    return asked


async def _approved(ctx: Context, contestant_id: uuid.UUID) -> Contestant | None:
    """The contestant's row, held until the unit of work ends, while they are
    approved; none once they are not, or when the row is gone.
    """
    contestant = (
        await ctx.db.execute(
            select(Contestant)
            .where(Contestant.id == contestant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if contestant is None or contestant.status != Status.APPROVED:
        return None
    return contestant


def _readiness(parts: Sequence[Provisioning]) -> Readiness:
    has_desk = any(part.kind == KIND for part in parts)
    ready = has_desk and all(part.status == provisioning.READY for part in parts)
    errors = [part.error for part in parts if part.status == provisioning.FAILED and part.error]
    return Readiness(
        WorkspaceState.READY if ready else WorkspaceState.PREPARING,
        errors[0] if errors else None,
    )


def _contestant_of(row: Provisioning) -> uuid.UUID:
    """The contestant a workspace's row or one of its places' rows is for."""
    return uuid.UUID(row.target_id.split(SEPARATOR, 1)[0])


def _place_target(contestant_id: uuid.UUID, task: TaskId) -> str:
    return f"{contestant_id}{SEPARATOR}{task}"


def _parse_place_target(target: str) -> tuple[uuid.UUID, TaskId]:
    contestant_id, task = target.split(SEPARATOR, 1)
    return uuid.UUID(contestant_id), TaskId(task)


async def place_ready(ctx: Context, contestant: Contestant, task: TaskId) -> bool:
    """Whether the contestant's desk is open and their place to submit the
    task is made.
    """
    if contestant.workspace_id is None:
        return False
    status = (
        await ctx.db.execute(
            select(Provisioning.status).where(
                Provisioning.kind == PLACE_KIND,
                Provisioning.target_id == _place_target(contestant.id, task),
            )
        )
    ).scalar_one_or_none()
    return status == provisioning.READY
