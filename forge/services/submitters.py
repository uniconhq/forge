"""Who is uploading or submitting to a task, and whether they may now. Every
upload slot and every submit starts here: the session is checked again, the
contest must be one the person sees and the task one released in it, read as
its latest publication froze it, and the entrant the person is in is read
beside it. `refuse` then applies the rules a submit is checked against
before anything is written, in this order, stopping at the first:

1. the task is open by the server's clock plus the entrant's extension on it
   (`task_closed`, or `archived` for a contest that is);
2. the person is in an approved entrant (`not_approved`);
3. the entrant still has as many members as the contest asks, since a
   member may have left it (`entrant_incomplete`).

A task that is not released, or a contest the person may not see, is no
such task, the same answer as one that is not there. An archived contest is
seen by nobody but its organisers, and still by everyone in an entrant of
it, who read their own submissions there and are told it is archived when
they submit.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from forge.db.tables import Entrant as EntrantRow
from forge.domain import entrants as entrant_rules
from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition, State
from forge.domain.errors import (
    Archived,
    EntrantChanged,
    NotApproved,
    NotFound,
    PortError,
    TaskClosed,
    Unavailable,
)
from forge.domain.ids import ContestId, TaskId, WorkspaceId
from forge.domain.names import EntrantOwner
from forge.domain.registration import Status
from forge.domain.release import Closed, Extension
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import entrants, places, published, release, sessions, timelines
from forge.services.published import PublishedTask
from forge.services.turns import Turns

log = get_logger(__name__)

NOT_APPROVED = "Only approved entrants of this contest can submit to its tasks."


@dataclass(frozen=True, slots=True)
class Person:
    """A signed-in person at a released task: their session, checked again,
    the contest's settings, the task as its latest publication froze it, the
    entrant they are in, if any, the workspace it owns, whose parts are made
    when they are first needed, every member of it, and when the person
    joined it, by the package's clock.
    """

    session: Session
    task: TaskId
    settings: ContestDefinition
    published: PublishedTask
    entrant: EntrantRow | None
    workspace: WorkspaceId | None
    members: tuple[int, ...] = ()
    since: tuple[datetime, uuid.UUID] | None = None


async def person(ctx: Context, session: Session, task: TaskId) -> Person:
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
    reader = await release.reader(ctx, fresh, contest)
    entered = settings.state is State.ARCHIVED and reader.entrant is not None
    if not release.sees(settings, reader) and not entered:
        raise NotFound(published.NO_SUCH_TASK)
    found = await published.task(ctx, task, settings)
    if found is None or not rules.released(settings, found.name, ctx.now):
        raise NotFound(published.NO_SUCH_TASK)
    standing = await entrants.standing(ctx, contest, fresh.user_id)
    if standing is None:
        return Person(fresh, task, settings, found, None, None)
    workspace = ctx.forge.workspaces.workspace_of(contest, standing.owner)
    return Person(
        fresh,
        task,
        settings,
        found,
        standing.entrant,
        workspace,
        standing.members,
        standing.since,
    )


async def refuse(ctx: Context, person: Person) -> tuple[EntrantRow, WorkspaceId]:
    """The approved entrant and its workspace, once the task is open to it;
    each rule's own refusal otherwise.
    """
    openness = rules.openness(person.settings, person.published.name, ctx.now, extension(person))
    match openness.reason:
        case Closed.NOT_RELEASED:
            raise NotFound(published.NO_SUCH_TASK)
        case Closed.ARCHIVED:
            raise Archived("This contest is archived, so it takes no more submissions.")
        case Closed.CLOSED:
            raise TaskClosed(
                "This task has closed for you, so it takes no more submissions from you.",
                reason=Closed.CLOSED.value,
            )
    row = person.entrant
    if row is None or row.status != Status.APPROVED:
        raise NotApproved(NOT_APPROVED)
    entrant_rules.refuse_incomplete(person.settings, len(person.members))
    assert person.workspace is not None
    return row, person.workspace


def extension(person: Person) -> Extension:
    """The extension of the entrant the person submits as, and none for one
    in no entrant.
    """
    if person.entrant is None:
        return Extension()
    return timelines.of_entrant(person.entrant)


async def hold_standing(ctx: Context, person: Person) -> Person:
    """Hold the person's entrant for reading until the unit of work ends, so
    no change of who is in it, and no change of its extension, passes this
    submit, and refuse with `EntrantChanged` when the person is no longer in
    it: a person joining, leaving or being moved waits for the submit, and a
    submit that started before such a change is asked again. The person
    comes back with the entrant's row and members read again under the
    hold, which `refuse` then checks: a change that committed before the
    hold, after the first read, is seen.
    """
    if person.entrant is None:
        return person
    row = await entrants.hold_for_reading(ctx, person.entrant.id)
    contest = contest_id_of(task_scope(person.task))
    member = await entrants.membership(ctx, contest, person.session.user_id)
    if row is None or member is None or member.entrant_id != row.id:
        raise EntrantChanged("Your team changed while you were submitting. Submit again.")
    members = tuple(await entrants.member_ids(ctx, row.id))
    return replace(person, entrant=row, members=members)


PLACES_AT_ONCE = 4
_room = Turns(PLACES_AT_ONCE)


async def open_place(ctx: Context, person: Person) -> None:
    """Make the entrant's place to submit the task, as the platform, and
    make sure it is still approved once it is made (`places.make`).

    The place is made first, holding nothing: making it takes the forge
    seconds in a rush, and a row held that long keeps a connection from
    everyone else, so a burst of first uploads once ran the pool dry. A
    removal can land while it is made, so the entrant's row is then held and
    read again, and one removed has the access just given taken away again
    before the person is refused. Whichever ends first, the access is gone
    once both have: a removal that held the row first is seen here and
    undone again, and one that comes after takes away a place that is
    already there.

    Both the first slot asked for a file and the first submit call this: an
    object belongs to a repository at the forge, so the place has to be there
    before any bytes can be sent, and making it twice is making it once. A
    place made ahead of time (`places`) costs one call to find finished.

    At most `PLACES_AT_ONCE` are made at a time in a process, the rest waiting
    their turn in order. Making one is about fifteen calls to the forge, and
    a burst of a hundred first uploads at once otherwise shares the forge
    client's few calls between them all, so every place is finished together
    at the end, past the proxy's minute; in turns, the first finish in
    seconds. One still waiting when the proxy gives up is made all the same,
    so asking again is answered at once.
    """
    user_id = person.session.user_id
    row = person.entrant
    if row is None or row.status != Status.APPROVED:
        raise NotApproved(NOT_APPROVED)
    try:
        approved = await places.make(
            ctx, ContestId(row.contest_id), row.id, person.task, _room(), for_user=user_id
        )
    except PortError as exc:
        log.warning(
            "submitters.place_failed",
            task=person.task,
            error=type(exc).__name__,
            detail=exc.detail,
        )
        raise Unavailable("The server did not answer. Try again in a moment.") from None
    if not approved:
        raise NotApproved(NOT_APPROVED)
    log.info("submitters.place_opened", task=person.task, user_id=user_id)


async def place_of(ctx: Context, task: TaskId, user_id: int) -> WorkspaceId:
    """The id of the workspace of the entrant the person is in for the task's
    contest. No call to the forge is made; the id comes from the contest and
    the entrant. `NotFound` for someone in no entrant.
    """
    contest = contest_id_of(task_scope(task))
    found = await entrants.membership(ctx, contest, user_id)
    if found is None:
        raise NotFound(published.NO_SUCH_TASK)
    return ctx.forge.workspaces.workspace_of(contest, EntrantOwner(found.entrant_id))
