"""Staff ending a contest's hold early, asked first, and a hold that has
not started or has already ended."""

import pytest
from sqlalchemy import func, select

from forge.db.tables import HoldEnd
from forge.domain.errors import ConfirmationRequired, Forbidden, NotFound
from forge.domain.roles import Role
from forge.runtime.setup import Setup
from forge.services import hold
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, organiser, write_contest
from tests.services.test_leaderboards import (
    CONTEST,
    HELD,
    HOLD_FROM,
    OPEN,
    _entrant,
    _held_contest,
    _standings,
    at,
)
from tests.services.test_pipelines import BEFORE_CLOSE, ISLANDS, _checked


async def test_the_hold_ends_at_its_until_and_ending_it_then_adds_nothing(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    until = HELD.replace("until: staff", "until: 2026-09-26T14:55:00Z")
    await _held_contest(setup, acme, clock, until)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    held = await _standings(setup, None)
    clock.set(at(14, 56))
    ended = await _standings(setup, None)

    assert [row.held for row in held.rows] == [1, 1]
    assert ended.held_from is None
    assert [row.held for row in ended.rows] == [0, 0]
    assert [row.cells[2].number for row in ended.rows] == [1, 2]
    assert await hold.end_hold(setup, manager, SPRING) is False
    state = await hold.hold_state(setup, manager, SPRING)
    assert (state.holding, state.ended_by) == (False, None)


async def _hold_ends(setup: Setup) -> int:
    async with setup.unit_of_work() as ctx:
        return await ctx.db.scalar(select(func.count()).select_from(HoldEnd)) or 0


async def test_a_manager_of_the_contest_ends_the_hold_early_asked_first_and_once(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    await _held_contest(setup, acme, clock, HELD)
    for user, scope, role in ((11, ISLANDS, Role.MANAGER), (12, CONTEST, Role.OBSERVER)):
        acme.fake.add_user(user, f"staff{user}")
        await acme.fake.orgs.grant_role(user, scope, role)
        refused = await organiser(setup, acme.fake, user, scope, role)
        with pytest.raises(Forbidden):
            await hold.end_hold(setup, refused, SPRING, confirmed=True)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    with pytest.raises(ConfirmationRequired) as asked:
        await hold.end_hold(setup, manager, SPRING)
    assert asked.value.extra == {"action": "end_hold"}
    assert [row.held for row in (await _standings(setup, None)).rows] == [1, 1]

    assert await hold.end_hold(setup, manager, SPRING, confirmed=True) is True
    ended = await _standings(setup, None)
    assert ended.held_from is None
    assert [row.held for row in ended.rows] == [0, 0]
    state = await hold.hold_state(setup, manager, SPRING)
    assert (state.from_, state.holding, state.ended_by, state.ended_by_name) == (
        HOLD_FROM,
        False,
        7,
        "ada",
    )
    assert state.ended_at == BEFORE_CLOSE
    # Ending it again answers the first end, which stays.
    assert await hold.end_hold(setup, manager, SPRING, confirmed=True) is False
    assert await _hold_ends(setup) == 1

    # A hold written again with a later `from` holds afresh.
    await write_contest(acme.fake, HELD.replace("14:00:00Z", "14:40:00Z"))
    again = await _standings(setup, None)
    assert again.held_from == at(14, 40)
    assert [row.held for row in again.rows] == [0, 1]
    assert (await hold.hold_state(setup, manager, SPRING)).holding

    await write_contest(acme.fake, OPEN)
    with pytest.raises(NotFound):
        await hold.end_hold(setup, manager, SPRING, confirmed=True)


async def test_ending_a_hold_before_its_from_adds_nothing_and_it_still_holds_from_then(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=HELD)
    await _entrant(setup, acme, islands, 20)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    # The hold has not started, so there is nothing to end: it answers as
    # it stands, asks nothing and adds no row.
    assert await hold.end_hold(setup, manager, SPRING) is False
    assert await _hold_ends(setup) == 0
    clock.set(at(14, 50))
    assert (await _standings(setup, None)).held_from == HOLD_FROM
    assert (await hold.hold_state(setup, manager, SPRING)).holding
