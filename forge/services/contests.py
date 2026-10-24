"""Making a contest and listing an org's contests. A contest is one place at
the forge holding `contest.yaml`, with three roles of its own. `create`
checks the name and makes it before it answers: the place with a starter
`contest.yaml` that is valid as written, then its roles and protection,
which `content.secure` attaches. A step that fails fails the request, which
writes nothing here, and the contest the try made is removed again with its
roles before the person is told; that is best effort, and a contest that
could not be removed is logged (`forge.services.making`). The person asks
again.

A contest needs the manager role at its org, which is also what shows the
org is there: roles are held at the forge, in the org. Its name is reserved
when it is asked for and it is made at the forge under its key, so the name
is only ever a label (`forge.services.names`).
"""

from dataclasses import dataclass
from datetime import datetime

from forge.domain.definitions import CONTEST_FILE, State, parse_contest, starter_contest, title_of
from forge.domain.errors import PortError
from forge.domain.identity import Identity
from forge.domain.ids import ContestId, OrgId
from forge.domain.names import Named, validate_contest_or_task_name
from forge.domain.roles import Role, Scope, contest_scope
from forge.domain.yaml_models import InvalidDefinition
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import making, names
from forge.services.access import Organiser, require

log = get_logger(__name__)


@action
async def create(
    ctx: Context, organiser: Organiser, org: OrgId, name: str, *, title: str | None = None
) -> Named:
    """Make the contest in the org, titled `title` or, without one, its name.
    Needs the manager role at the org. `InvalidName` for a name that breaks
    the rules; `Conflict` when it is taken.
    """
    require(organiser, Scope(org), Role.MANAGER)
    validate_contest_or_task_name(name)
    contest = await names.reserve_contest(ctx, org, name)
    scope = contest_scope(contest)
    starter = {CONTEST_FILE: starter_contest(title_of(title, name), ctx.now)}
    made = making.undo_on_rollback(ctx, "contests", contest=contest)
    try:
        await making.place(
            made,
            "contest",
            contest,
            ctx.forge.content.create_contest(org, str(scope.contest), starter),
            lambda: ctx.forge.content.delete_place(contest),
        )
        await ctx.forge.content.secure(contest)
    except PortError as exc:
        raise making.failure(exc, "contests.step_failed", contest=contest) from None
    log.info("contests.created", contest=contest, user_id=organiser.user.id)
    return Named(contest, name)


@dataclass(frozen=True, slots=True)
class Listed:
    """A contest in its org's list: its id and name, and its state, start and
    end as its `contest.yaml` says them, all three none when the file cannot
    be read or does not validate.
    """

    id: str
    name: str
    state: State | None
    start: datetime | None
    end: datetime | None


@action
async def list_contests(ctx: Context, organiser: Organiser, org: OrgId) -> tuple[Listed, ...]:
    """Every contest in the org with its name, by name, and its state and
    dates, read as the organiser, so they see the ones the forge lets them
    read. Needs the observer role at the org. The connection is let go of
    before the contests' settings are read, one forge read each.
    """
    require(organiser, Scope(org), Role.OBSERVER)
    found = await names.named(ctx, await ctx.forge.content.list_contests(organiser.identity, org))
    await ctx.let_go()
    return tuple([await _listed(ctx, organiser.identity, each) for each in found])


async def _listed(ctx: Context, as_: Identity, contest: Named) -> Listed:
    """The contest with its state, start and end, or none of them when its
    settings cannot be read or do not validate.
    """
    try:
        found = await ctx.forge.content.read_file(as_, ContestId(contest.id), CONTEST_FILE)
        settings = parse_contest(found.content)
    except PortError, InvalidDefinition:
        return Listed(contest.id, contest.name, None, None, None)
    return Listed(contest.id, contest.name, settings.state, settings.start, settings.end)
