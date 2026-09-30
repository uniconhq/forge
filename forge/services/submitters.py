"""Who is uploading or submitting to a task, and whether they may now. Every
upload slot and every submit starts here: the session is checked again, the
contest must be one the person sees and the task one released in it, read as
its latest publication froze it, and the person's own registration is read
beside it. `refuse` then applies the rules a submit is checked against
before anything is written, in this order, stopping at the first:

1. the task is open by the server's clock plus the person's own extension
   (`task_closed`, or `archived` for a contest that is);
2. they are an approved contestant (`not_approved`);
3. their desk is open and their place to submit the task is made
   (`workspace_not_ready`).

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
from forge.domain.errors import Archived, NotApproved, NotFound, TaskClosed, WorkspaceNotReady
from forge.domain.ids import TaskId, WorkspaceId
from forge.domain.registration import Status
from forge.domain.release import Closed
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.runtime.context import Context
from forge.services import contestants, published, release, sessions, workspaces
from forge.services.published import PublishedTask


@dataclass(frozen=True, slots=True)
class Entrant:
    """A signed-in person at a released task: their session, checked again,
    the contest's settings, the task as its latest publication froze it, and
    their registration for the contest, if any.
    """

    session: Session
    task: TaskId
    settings: ContestDefinition
    published: PublishedTask
    row: Contestant | None

    @property
    def workspace(self) -> WorkspaceId | None:
        """The workspace the registration names, once its desk is open."""
        if self.row is None or self.row.workspace_id is None:
            return None
        return WorkspaceId(self.row.workspace_id)


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
    return Entrant(fresh, task, settings, found, person.row)


async def refuse(ctx: Context, entrant: Entrant) -> tuple[Contestant, WorkspaceId]:
    """The approved contestant's row and workspace, once the task is open to
    them and their place to submit it is made; each rule's own refusal
    otherwise.
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
        raise NotApproved("Only an approved contestant of the contest submits to its tasks.")
    workspace = entrant.workspace
    if workspace is None or not await workspaces.place_ready(ctx, row, entrant.task):
        raise WorkspaceNotReady("Your place to submit this task is still being made.")
    return row, workspace
