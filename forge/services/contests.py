"""Making a contest, listing an org's contests, and following how far making
one has got. A contest is one place at the forge holding `contest.yaml`,
with three roles of its own. `create` checks the name and writes the request
and answers at once; the `provisioning` poller runs `provision`, the two
recorded steps that make it: the place with a starter `contest.yaml` that is
valid as written, and its roles and protection, which `content.secure`
attaches and checks, so the nightly pass can put them back.

A contest needs the manager role at its org, which is also what shows the
org is there: roles are held at the forge, in the org.
"""

from forge.db.tables import Provisioning
from forge.domain.definitions import CONTEST_FILE, starter_contest, title_of
from forge.domain.errors import Conflict
from forge.domain.ids import ContestId, OrgName
from forge.domain.names import validate_contest_or_task_name
from forge.domain.roles import Role, Scope, contest_id_of, contest_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import provisioning
from forge.services.access import Organiser, require
from forge.services.provisioning import Attempt, Record

log = get_logger(__name__)

KIND = "contest"


@action
async def create(
    ctx: Context, organiser: Organiser, org: OrgName, name: str, *, title: str | None = None
) -> Record:
    """Ask for the contest to be made in the org, titled `title` or, without
    one, its name. Needs the manager role at the org. `InvalidName` for a
    name that breaks the rules; `Conflict` when it is taken or being made.
    """
    require(organiser, Scope(org), Role.MANAGER)
    validate_contest_or_task_name(name)
    contest = contest_id_of(Scope(org, name))
    row = await provisioning.request(
        ctx, KIND, contest, {"title": title_of(title, name), "creator_user_id": organiser.user.id}
    )
    log.info("contests.requested", contest=contest, user_id=organiser.user.id)
    return provisioning.record_from(row)


async def provision(ctx: Context, row: Provisioning) -> None:
    """The poller's work over a contest's row: the steps, in order, each safe
    to run again. A contest the first try made before its record caught up
    is taken as made.
    """
    contest = ContestId(row.target_id)
    scope = contest_scope(contest)
    name = str(scope.contest)
    title = str(row.payload.get("title") or name)

    async def make_repo(attempt: Attempt) -> None:
        try:
            await ctx.forge.content.create_contest(
                OrgName(scope.org), name, {CONTEST_FILE: starter_contest(title, ctx.now)}
            )
        except Conflict:
            if not attempt.is_rerun:
                raise

    async def make_roles(attempt: Attempt) -> None:
        await ctx.forge.content.secure(contest)

    await provisioning.run(
        ctx, row, provisioning.steps(KIND, {"repo": make_repo, "roles": make_roles})
    )


@action
async def status(ctx: Context, organiser: Organiser, contest: ContestId) -> Record | None:
    """Where making the contest has got to, for anyone observing its org, or
    none when nothing has asked for it.
    """
    require(organiser, Scope(contest_scope(contest).org), Role.OBSERVER)
    return await provisioning.record_of(ctx, KIND, contest)


@action
async def list(ctx: Context, organiser: Organiser, org: OrgName) -> tuple[ContestId, ...]:
    """Every contest in the org, by name, read as the organiser, so they see
    the ones the forge lets them read. Needs the observer role at the org.
    """
    require(organiser, Scope(org), Role.OBSERVER)
    return await ctx.forge.content.list_contests(organiser.identity, org)
