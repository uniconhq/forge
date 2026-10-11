"""Whether the signed-in person sees a task and may submit to it, worked out
on every read from the settings and the clock by the rules in
`forge.domain.release`. The contest's settings are its `contest.yaml` now,
where each task's entry is its timeline; the extension is the one of the
entrant the person is in (`forge.services.timelines`), and outside one they
have none. Everything is read as the
platform, since a contestant can read neither, so the contest's own
`visibility` is applied first: a person the contest is hidden from is told
there is no such task. A task with no publication is not released. Nothing at
the forge changes when a task becomes released.
"""

from dataclasses import dataclass
from datetime import datetime

from forge.db.tables import Entrant
from forge.domain import release as rules
from forge.domain.definitions import ContestDefinition
from forge.domain.errors import NotFound
from forge.domain.ids import ContestId, TaskId
from forge.domain.registration import Status
from forge.domain.release import Closed, Extension
from forge.domain.roles import contest_id_of, task_scope
from forge.domain.sessions import Session
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrants, invites, published, roles, timelines


@dataclass(frozen=True, slots=True)
class TaskRelease:
    """Whether the task is released, visible and open to one person now, and
    when it is not open, the first reason why.
    """

    released: bool
    visible: bool
    open: bool
    closed: Closed | None


NOT_RELEASED = TaskRelease(released=False, visible=False, open=False, closed=Closed.NOT_RELEASED)


@action
async def of_task(ctx: Context, session: Session, task: TaskId) -> TaskRelease:
    """Whether the task is released, visible and open to the signed-in person
    now, by the server's clock. `NotFound` when the contest is hidden from
    them.
    """
    try:
        settings, person = await seen(ctx, session, contest_id_of(task_scope(task)))
    except NotFound as exc:
        raise NotFound(published.NO_SUCH_TASK) from exc
    found = await published.task(ctx, task, settings)
    if found is None:
        return NOT_RELEASED
    extension = row_extension(person)
    return of(settings, found.name, ctx.now, extension)


def row_extension(person: Reader) -> Extension:
    """The extension of the entrant the person is in, and none for a person
    in no entrant.
    """
    return timelines.of_entrant(person.entrant)


def of(settings: ContestDefinition, task: str, now: datetime, extension: Extension) -> TaskRelease:
    """Where the task, by its name, stands at `now` for a row with its own
    `extension`.
    """
    openness = rules.openness(settings, task, now, extension)
    return TaskRelease(
        released=rules.released(settings, task, now),
        visible=rules.visible(settings, task, now),
        open=openness.open,
        closed=openness.reason,
    )


@dataclass(frozen=True, slots=True)
class Reader:
    """A signed-in person as a contest's reads see them: the entrant they are
    in, if any, whether they hold a role at it, at one of its tasks or at its
    org, and whether they have accepted an organiser's invite to take part
    and are in no entrant yet, which shows them a hidden contest as its
    contestants see it; once they are in an entrant, its status decides.
    """

    entrant: Entrant | None
    organises: bool
    invited: bool = False


async def reader(ctx: Context, session: Session, contest: ContestId) -> Reader:
    """The signed-in person as the contest's reads see them, their roles read
    once.
    """
    entrant = await entrants.of_person(ctx, contest, session.user_id)
    invited = entrant is None and await invites.accepted_place(ctx, contest, session.user_id)
    await ctx.let_go()
    return Reader(
        entrant=entrant,
        organises=await roles.holds_role_in_contest(ctx, session.user_id, contest),
        invited=invited,
    )


async def seen(
    ctx: Context, session: Session, contest: ContestId
) -> tuple[ContestDefinition, Reader]:
    """The contest's settings and the person as its reads see them, once the
    contest is one they see. `NotFound` otherwise, the same as for a contest
    that is not there.
    """
    settings = await published.contest(ctx, contest)
    person = await reader(ctx, session, contest)
    await ctx.let_go()
    if not sees(settings, person):
        raise NotFound(published.NO_SUCH_CONTEST)
    return settings, person


def sees(settings: ContestDefinition, person: Reader) -> bool:
    """Whether the person sees the contest at all: organisers always, and
    anyone else as its visibility says.
    """
    return rules.contest_visible_to(
        settings,
        has_session=True,
        is_contestant=person.invited
        or (person.entrant is not None and person.entrant.status == Status.APPROVED),
        is_organiser=person.organises,
    )
