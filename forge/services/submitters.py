"""Who is uploading or submitting to a task, and whether they may now. Every
upload slot and every submit starts here: the session is checked again, the
contest must be one the person sees and the task one released in it, read as
its latest publication froze it, and the person's own registration is read
beside it. `refuse` then applies the rules a submit is checked against
before anything is written, in this order, stopping at the first:

1. the task is open by the server's clock plus the person's own extension
   (`task_closed`, or `archived` for a contest that is);
2. they are an approved contestant (`not_approved`).

A task that is not released, or a contest the person may not see, is no
such task, the same answer as one that is not there. An archived contest is
seen by nobody but its organisers, and still by everyone who entered it, who
read their own submissions there and are told it is archived when they
submit.
"""

from dataclasses import dataclass

from forge.db.tables import Contestant
from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import (
    Archived,
    NotApproved,
    NotFound,
    PortError,
    TaskClosed,
    Unavailable,
)
from forge.domain.ids import TaskId, WorkspaceId
from forge.domain.names import UserOwner
from forge.domain.registration import Status
from forge.domain.release import Closed
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import contestants, published, release, sessions, workspaces
from forge.services.published import PublishedTask

log = get_logger(__name__)

NOT_APPROVED = "Only an approved contestant of the contest submits to its tasks."


@dataclass(frozen=True, slots=True)
class Entrant:
    """A signed-in person at a released task: their session, checked again,
    the contest's settings, the task as its latest publication froze it,
    their registration for the contest, if any, and the workspace it gives
    them, whose parts are made when they are first needed.
    """

    session: Session
    task: TaskId
    settings: ContestDefinition
    published: PublishedTask
    row: Contestant | None
    workspace: WorkspaceId | None


async def entrant(ctx: Context, session: Session, task: TaskId) -> Entrant:
    """The person behind `session` at `task`. `Unauthenticated` or
    `SessionExpired` for a session that has ended, and `NotFound` for a task
    that is not released to them or in a contest they may not see.
    """
    fresh = await sessions.authenticate(ctx, session.id)
    contest = contest_id_of(task_scope(task))
    try:
        settings = await published.contest(ctx, contest)
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    person = await release.reader(ctx, fresh, contest)
    entered = settings.state is State.ARCHIVED and person.row is not None
    if not release.sees(settings, person) and not entered:
        raise NotFound(published.NO_SUCH_TASK)
    found = await published.task(ctx, task, settings)
    if found is None or not rules.released(settings, found.definition, ctx.now):
        raise NotFound(published.NO_SUCH_TASK)
    workspace = (
        ctx.forge.workspaces.workspace_of(contest, UserOwner(fresh.user_id))
        if person.row is not None
        else None
    )
    return Entrant(fresh, task, settings, found, person.row, workspace)


async def refuse(ctx: Context, entrant: Entrant) -> tuple[Contestant, WorkspaceId]:
    """The approved contestant's row and workspace, once the task is open to
    them; each rule's own refusal otherwise.
    """
    openness = rules.openness(
        entrant.settings,
        entrant.published.definition,
        ctx.now,
        contestants.time_extension(entrant.row),
    )
    match openness.reason:
        case Closed.NOT_RELEASED:
            raise NotFound(published.NO_SUCH_TASK)
        case Closed.ARCHIVED:
            raise Archived("The contest is archived and takes no submissions.")
        case Closed.ENDED:
            raise TaskClosed("The contest has ended for you.", reason=Closed.ENDED.value)
        case Closed.SUBMISSIONS_CLOSED:
            raise TaskClosed(
                "The organisers have closed submissions.",
                reason=Closed.SUBMISSIONS_CLOSED.value,
            )
    row = entrant.row
    if row is None or row.status != Status.APPROVED:
        raise NotApproved(NOT_APPROVED)
    assert entrant.workspace is not None
    return row, entrant.workspace


async def open_place(ctx: Context, entrant: Entrant, workspace: WorkspaceId) -> None:
    """Make the contestant's place to submit the task, as the platform, and
    make sure they are still approved once it is made.

    The place is made first, holding nothing: making it takes the forge
    seconds, and a row held that long keeps a connection from everyone else,
    so a burst of first uploads once ran the pool dry. A removal can land
    while it is made, so the person's row is then held and read again, and
    one no longer approved has the access just given taken away again before
    they are refused. Whichever ends first, the access is gone once both
    have: a removal that held the row first is seen here and undone again,
    and one that comes after takes away a place that is already there.

    Both the first slot asked for a file and the first submit call this: an
    object belongs to a repository at the forge, so the place has to be there
    before any bytes can be sent, and making it twice is making it once.
    """
    user_id = entrant.session.user_id
    row = entrant.row
    if row is None or row.status != Status.APPROVED:
        raise NotApproved(NOT_APPROVED)
    await ctx.let_go()
    try:
        await ctx.forge.workspaces.open_submission_place(workspace, entrant.task, [user_id])
    except PortError as exc:
        log.warning(
            "submitters.place_failed",
            task=entrant.task,
            error=type(exc).__name__,
            detail=exc.detail,
        )
        raise Unavailable("The forge did not answer; try again in a moment.") from None
    await ctx.db.refresh(row, with_for_update=True)
    if row.status != Status.APPROVED:
        await workspaces.close(ctx, row)
        log.info("submitters.place_taken_back", task=entrant.task, user_id=user_id)
        raise NotApproved(NOT_APPROVED)
    log.info("submitters.place_opened", task=entrant.task, user_id=user_id)


def place_of(ctx: Context, task: TaskId, user_id: int) -> WorkspaceId:
    """The id of the person's workspace in the task's contest. No call is
    made; the id comes from the contest and the person.
    """
    return ctx.forge.workspaces.workspace_of(contest_id_of(task_scope(task)), UserOwner(user_id))
