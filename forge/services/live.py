"""Live updates: the stream a signed-in session reads nudges from, and what
it may hear.

`stream` is what the host serves as one Server-Sent Events connection per
open tab. It checks the session, reads who the session speaks for
(`audience`: the person, every role they hold, the teams they are in and the contests where they
are an approved contestant), subscribes to this process's broker and hands
on each nudge that audience hears, or `None` every `HEARTBEAT` so the host
can write a keepalive and find out whether the browser is still there. Its
first item is a `None` as soon as it has subscribed, so a host that waits
for it has every refusal raised before it answers. It holds no connection
while it waits. While it stays open it checks the session again every
`RECHECK`, without counting that as the person being there, and ends when
the session has; and it reads the audience again every `AUDIENCE_KEPT`, so
a role taken away or a registration approved reaches the stream within
minutes. A forge that does not answer then leaves the audience as it was.
The broker ends a session's oldest stream once it holds too many. Nothing
it sends carries anything a person could read: a nudge is a kind and an
id.

Nudges are published by the units of work that make the change
(`Context.nudge`): a grading's status changes in `gradings` and `runs`, and
a thread's in `events`, from the forge's push.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator

from sqlalchemy import select

from forge.db.tables import Contestant
from forge.domain.errors import Forbidden, SessionExpired, Unauthenticated, Unavailable
from forge.domain.identity import AsUser
from forge.domain.ids import ContestId
from forge.domain.live import Audience, Nudge
from forge.domain.registration import Status
from forge.domain.sessions import Session
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import ActionSetup, Context
from forge.runtime.held import setup_or_held
from forge.services import identity, sessions, teams

log = get_logger(__name__)

HEARTBEAT = 15.0
RECHECK = 60.0
AUDIENCE_KEPT = 300.0


@action
async def audience(ctx: Context, session: Session, previous: Audience | None = None) -> Audience:
    """Who the session speaks for: the contests where they are an approved
    contestant, read here, and the roles they hold, read at the forge with
    their own credential once the connection is let go of. A forge that does
    not answer, or a credential that has expired while only the stream was
    open, leaves the roles `previous` held, or none.
    """
    contests = frozenset(
        ContestId(found)
        for found in (
            await ctx.db.scalars(
                select(Contestant.contest_id).where(
                    Contestant.user_id == session.user_id,
                    Contestant.status == Status.APPROVED,
                )
            )
        )
    )
    in_teams = await teams.team_ids(ctx, session.user_id)
    grants = previous.grants if previous is not None else ()
    try:
        credential = await sessions.credential_for(ctx, session.id)
    except Unavailable:
        return Audience(user_id=session.user_id, grants=grants, contests=contests, teams=in_teams)
    await ctx.let_go()
    try:
        grants = tuple(await ctx.forge.orgs.roles_of(AsUser(session.user_id, credential)))
    except Unavailable:
        pass
    except Forbidden:
        sessions.revoke_at_end(ctx, session.id)
        raise SessionExpired("Sign in again.") from None
    return Audience(
        user_id=session.user_id, grants=tuple(grants), contests=contests, teams=in_teams
    )


async def stream(
    session_id: uuid.UUID,
    *,
    setup: ActionSetup | None = None,
    heartbeat: float = HEARTBEAT,
    recheck: float = RECHECK,
    refresh: float = AUDIENCE_KEPT,
) -> AsyncGenerator[Nudge | None]:
    """The nudges the session hears, and `None` every `heartbeat` seconds
    with nothing to say, until the session ends or the caller stops
    reading. The first check of the session raises as the guard does; a
    session that ends while the stream is open ends it quietly.
    """
    on = setup_or_held(setup)
    session = await identity.current(session_id, setup=on)
    async with on.broker.subscription(
        await audience(on, session), session=session_id
    ) as subscription:
        yield None
        checked = refreshed = time.monotonic()
        while True:
            try:
                nudge: Nudge | None = await _next(subscription.queue, heartbeat)
            except TimeoutError:
                nudge = None
            if subscription.ended:
                log.info("live.replaced", session=str(session_id))
                return
            now = time.monotonic()
            try:
                if now - checked >= recheck:
                    checked = now
                    session = await _still_on(on, session_id)
                if now - refreshed >= refresh:
                    refreshed = now
                    subscription.audience = await audience(on, session, subscription.audience)
            except Unauthenticated, SessionExpired:
                log.info("live.session_ended", session=str(session_id))
                return
            yield nudge


@action
async def _still_on(ctx: Context, session_id: uuid.UUID) -> Session:
    session, _ = await sessions.check(ctx, session_id, touch=False)
    return session


async def _next(queue: asyncio.Queue[Nudge], within: float) -> Nudge:
    async with asyncio.timeout(within):
        return await queue.get()
