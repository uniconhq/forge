"""The nightly drift pass: what the package checks at the forge and the CI
once a day, whatever anyone clicked. It makes three checks. Every org account
is still in its place in its org, and the CI still answers it. And every org
the platform made has its roles made again, which gives any team whose
permission changed at the forge its own back, and every contest and task in
it is secured again through `content.secure`, which puts back any of its
roles, its protected history or, for a task, its reserved publications that
went missing, so a failure halfway through provisioning, or a team detached
at the forge, does not leave a contest nobody below the org can reach.
"""

from forge.domain.errors import PortError
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.log import get_logger
from forge.runtime.context import Context
from forge.services import org_accounts

log = get_logger(__name__)


async def nightly(ctx: Context) -> None:
    """A timed pass, run on the pass's unit of work under its advisory lock."""
    restored = await org_accounts.restore_membership(ctx)
    alive = await org_accounts.keepalive(ctx)
    secured = await secure_content(ctx)
    log.info(
        "drift.nightly",
        membership_restored=restored,
        accounts_alive=alive,
        content_restored=secured,
    )


async def secure_content(ctx: Context) -> int:
    """Secure every contest and task of every org with an account row again,
    and return how many things had to be put back. A place the forge fails on
    is logged and the pass goes on.
    """
    put_back = 0
    for org in await org_accounts.org_names(ctx):
        try:
            await ctx.forge.orgs.create_roles(org)
            contests = await ctx.forge.content.list_contests(PLATFORM, org)
        except PortError as exc:
            log.warning("drift.org_unreadable", org=org, error=type(exc).__name__, detail=str(exc))
            continue
        for contest in contests:
            put_back += await _secured(ctx, contest)
            try:
                tasks = await ctx.forge.content.list_tasks(PLATFORM, contest)
            except PortError as exc:
                log.warning(
                    "drift.contest_unreadable",
                    contest=contest,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                continue
            for task in tasks:
                put_back += await _secured(ctx, task)
    return put_back


async def _secured(ctx: Context, place: ContestId | TaskId) -> int:
    try:
        put_back = await ctx.forge.content.secure(place)
    except PortError as exc:
        log.warning("drift.secure_failed", place=place, error=type(exc).__name__, detail=str(exc))
        return 0
    if put_back:
        log.warning("drift.content_restored", place=place, put_back=put_back)
    return put_back
