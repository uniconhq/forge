"""The contest's hold, where it stands, and a manager ending it early."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select

from forge.db.tables import HoldEnd
from forge.domain.definitions import Hold
from forge.domain.errors import ConfirmationRequired, NotFound
from forge.domain.ids import ContestId
from forge.domain.leaderboards import holding
from forge.domain.release import in_words
from forge.domain.roles import Role, contest_scope
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import entrants, published, timelines
from forge.services.access import Organiser, require

log = get_logger(__name__)

NO_HOLD: Final = "The contest has no hold to end."


@dataclass(frozen=True, slots=True)
class HoldState:
    """Where the contest's hold stands: its `from` and `until`, none for
    each with no hold; whether it holds now; and, when staff ended it
    early, who, by user id and `by_name`, their username, none when the
    forge does not say, and when."""

    from_: datetime | None
    until: datetime | str | None
    holding: bool
    ended_by: int | None = None
    ended_by_name: str | None = None
    ended_at: datetime | None = None


async def _hold_end(ctx: Context, contest: ContestId, hold: Hold | None) -> HoldEnd | None:
    """The row ending the contest's hold early, none when staff have not
    ended this hold, the one with this `from`."""
    if hold is None:
        return None
    return await ctx.db.scalar(
        select(HoldEnd).where(HoldEnd.contest_id == contest, HoldEnd.hold_from == hold.from_)
    )


async def hold_ended(ctx: Context, contest: ContestId, hold: Hold | None) -> bool:
    return await _hold_end(ctx, contest, hold) is not None


@action
async def hold_state(ctx: Context, organiser: Organiser, contest: ContestId) -> HoldState:
    """Where the contest's hold stands, for an observer of the contest, as
    ending it early answers once that has committed."""
    require(organiser, contest_scope(contest), Role.OBSERVER)
    settings = await published.contest(ctx, contest)
    hold = settings.hold
    if hold is None:
        return HoldState(None, None, holding=False)
    row = await _hold_end(ctx, contest, hold)
    on = holding(hold, ctx.now, row is not None) is not None
    if row is None:
        return HoldState(hold.from_, hold.until, on)
    name = await entrants.username_of(ctx, row.ended_by) or None
    return HoldState(hold.from_, hold.until, on, row.ended_by, name, row.ended_at)


@action
async def end_hold(
    ctx: Context, organiser: Organiser, contest: ContestId, *, confirmed: bool = False
) -> bool:
    """End the contest's hold early, for a manager or admin of the contest:
    from now on every viewer reads every row as it stands. Without
    `confirmed`, nothing changes and `ConfirmationRequired` says what
    becomes visible and that it cannot be undone for this hold. A hold
    already ended, by staff or at its `until`, or not started yet, stays as
    it is, with no row added and nothing asked. `NotFound` for a contest
    with no hold. Whether a row was added; `hold_state` reads the outcome.
    """
    require(organiser, contest_scope(contest), Role.MANAGER)
    # A save of the settings in flight cannot move the hold's `from` under it.
    await timelines.hold_rules(ctx, contest)
    settings = await published.contest(ctx, contest)
    hold = settings.hold
    if hold is None:
        raise NotFound(NO_HOLD)
    if await hold_ended(ctx, contest, hold):
        return False
    if holding(hold, ctx.now, ended=False) is None:
        # Not started yet, or past its `until`: nothing to end.
        return False
    if not confirmed:
        raise ConfirmationRequired(_end_words(hold), action="end_hold")
    ctx.db.add(
        HoldEnd(
            contest_id=contest,
            hold_from=hold.from_,
            ended_by=organiser.user.id,
            ended_at=ctx.now,
        )
    )
    await ctx.db.flush()
    log.info("boards.hold_ended", contest=contest, by=organiser.user.id)
    return True


def _end_words(hold: Hold) -> str:
    return (
        "Every leaderboard will show every viewer each row as it stands, with the submissions "
        f"made since {in_words(hold.from_)} and the marks on other rows. This hold cannot be "
        "put back; a hold written again with another from holds afresh."
    )
