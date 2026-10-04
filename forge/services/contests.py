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

from forge.domain.definitions import CONTEST_FILE, starter_contest, title_of
from forge.domain.errors import PortError
from forge.domain.ids import OrgId
from forge.domain.names import Named, validate_contest_or_task_name
from forge.domain.roles import Role, Scope, contest_scope
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


@action
async def list(ctx: Context, organiser: Organiser, org: OrgId) -> tuple[Named, ...]:
    """Every contest in the org with its name, by name, read as the
    organiser, so they see the ones the forge lets them read. Needs the
    observer role at the org.
    """
    require(organiser, Scope(org), Role.OBSERVER)
    return await names.named(ctx, await ctx.forge.content.list_contests(organiser.identity, org))
