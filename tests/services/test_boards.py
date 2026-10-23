"""The contest's leaderboards as each viewer reads them (SIMPLER-CORE.md
10.2, the leaderboards note's 6.3): a visitor and an entrant read a board
for everyone with each row's values as that row's entrant is shown them,
and only their own row of the gradebook; a withdrawn task's columns are
listed to no entrant; the hold keeps every row but the viewer's own as it
stood at its `from`, counting the rest held, until its `until` or a
manager of the contest ends it early, asked first, the end kept with who
and when, a hold written again with a later `from` holding afresh; and the
contest home shows an entrant their own row of each board they read.
"""

import uuid
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import func, select

from forge.db.tables import Grading, HoldEnd
from forge.domain.boards import Board, BoardCell, BoardRow, Counted
from forge.domain.errors import ConfirmationRequired, Forbidden, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import boards, contest_home, submissions
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, organiser, signed_in, upload, write_contest
from tests.services.test_pipelines import (
    ACCEPTED_EARLY,
    ACCEPTED_LATE,
    AFTER_CLOSE,
    BEFORE_CLOSE,
    ISLANDS,
    LIVE,
    _checked,
    _ended,
    _rows,
    _staff,
    _submit,
)
from tests.services.test_steps import CODE, Islands

CONTEST = Scope("acme", "spring")
BOARDS = """\
leaderboards:
  standings:
    title: Standings
    audience: {who: everyone}
    columns:
      sample:
        block: otherwise
        of: [{value: [{block: latest, from: {results: islands}}, sample_score]}, 0]
      total:
        block: otherwise
        of: [{value: [{block: latest, from: {results: islands}}, total]}, 0]
      tries: {block: count, over: {results: islands}}
    sort: [{sample: desc}, {tries: asc}]
    display: [sample, total, tries]
  gradebook:
    audience: {who: entrants, rows: own}
    columns:
      total:
        block: otherwise
        of: [{value: [{block: latest, from: {results: islands}}, total]}, 0]
    sort: [{total: desc}]
    display: [total]
"""
"""A board for everyone over islands' sample score, shown at once, and its
total, shown after the close; and a gradebook each entrant reads alone."""

OPEN = LIVE + BOARDS
HOLD_FROM = datetime(2026, 9, 26, 14, tzinfo=UTC)
HELD = LIVE + "hold: {from: 2026-09-26T14:00:00Z, until: staff}\n" + BOARDS
"""The boards, held from 14:00 until staff end it."""


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 26, hour, minute, tzinfo=UTC)


async def _graded(setup: Setup) -> None:
    """Every early row still running ended with every run accepted."""
    for row in await _rows(setup, "early"):
        if row.status is not GradingStatus.DONE:
            await _ended(setup, row, ACCEPTED_EARLY, ACCEPTED_LATE)


async def _entrant(setup: Setup, acme: Acme, islands: Islands, user: int) -> Any:
    """A new entrant, signed in as `user`, named for it, with one
    submission of islands graded."""
    acme.fake.add_user(user, f"entrant{user}")
    session = await _submit(setup, acme, islands, user=user)
    await _graded(setup)
    return session


async def _again(setup: Setup, acme: Acme, islands: Islands, session: Any) -> None:
    """Another submission of islands by the entrant, graded."""
    made = await upload(setup, acme.fake, session, islands.task, CODE, input="code")
    await submissions.submit(
        setup,
        session,
        islands.task,
        {"code": SubmittedInput(uploads=(made.id,)), "language": SubmittedInput(value="python")},
        idempotency_key=uuid.uuid4().hex,
    )
    await setup.settle()
    await _graded(setup)


def _cells(row: BoardRow) -> list[tuple[str, Fraction | None, bool]]:
    return [(cell.column, cell.number, cell.hidden) for cell in row.cells]


def _rows_of(board: Board) -> list[tuple[str, int | None, int]]:
    return [(row.name, row.place, row.held) for row in board.rows]


async def _standings(setup: Setup, session: Any) -> Board:
    found = await boards.seen(setup, session, SPRING, "standings")
    assert len(found) == 1
    return found[0]


async def test_everyone_reads_each_row_as_its_own_entrant_is_shown_it(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=OPEN)
    first = await _entrant(setup, acme, islands, 20)
    await _entrant(setup, acme, islands, 21)
    clock.set(BEFORE_CLOSE)
    staff = await _staff(setup, acme)

    visitor = await boards.seen(setup, None, SPRING)
    entrant = await boards.seen(setup, first, SPRING)
    [everyone, staffs] = await boards.seen(setup, staff, SPRING)

    # A visitor reads the board for everyone and no gradebook. The total
    # is shown only after the close, so it is hidden on every row, and
    # what each cell counted is told to nobody but the row's own entrant.
    assert [board.key for board in visitor] == ["standings"]
    [standings] = visitor
    assert _rows_of(standings) == [("entrant20", 1, 0), ("entrant21", 1, 0)]
    for row in standings.rows:
        assert _cells(row) == [
            ("sample", Fraction(1), False),
            ("total", None, True),
            ("tries", Fraction(1), False),
        ]
        assert not row.told and row.cells[0].counted == ()
    # An entrant also reads their own gradebook row, with no place, and is
    # told what their own cells counted.
    assert [board.key for board in entrant] == ["standings", "gradebook"]
    mine, other = entrant[0].rows
    assert mine.told and mine.cells[0].counted == (Counted("islands", 1),)
    assert not other.told
    [row] = entrant[1].rows
    assert (row.name, row.place, row.cells) == (
        "entrant20",
        None,
        (BoardCell("total", None, hidden=True),),
    )
    # Staff read every value, of both boards.
    assert [cell.number for cell in everyone.rows[0].cells] == [1, 100, 1]
    assert [len(staffs.rows), staffs.rows[0].cells[0].number] == [2, 100]

    clock.set(AFTER_CLOSE)
    after = await _standings(setup, None)
    assert [cell.number for cell in after.rows[1].cells] == [1, 100, 1]


async def test_a_withdrawn_tasks_columns_are_listed_to_no_entrant_and_read_nothing(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=OPEN)
    first = await _entrant(setup, acme, islands, 20)
    await write_contest(
        acme.fake, OPEN.replace("  - id: islands\n", "  - {id: islands, withdrawn: true}\n")
    )
    staff = await _staff(setup, acme)

    entrant = await _standings(setup, first)
    staffs = await _standings(setup, staff)

    assert (entrant.headers, entrant.rows[0].cells, entrant.rows[0].place) == ((), (), None)
    assert entrant.dropped == ("sample", "tries")
    # Staff still read the columns, over no results.
    assert [header.name for header in staffs.headers] == ["sample", "total", "tries"]
    assert [cell.number for cell in staffs.rows[0].cells] == [0, 0, 0]


async def _held_contest(setup: Setup, acme: Acme, clock: FakeClock, contest: str) -> Any:
    """entrant20 submitting at 13:00 and 14:30, entrant21 at 14:45, each
    graded; both sessions, read at 14:50."""
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=contest)
    first = await _entrant(setup, acme, islands, 20)
    clock.set(at(14, 30))
    await _again(setup, acme, islands, first)
    clock.set(at(14, 45))
    second = await _entrant(setup, acme, islands, 21)
    clock.set(BEFORE_CLOSE)
    return first, second


async def test_the_hold_keeps_other_rows_as_they_stood_at_from_and_counts_the_rest(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    first, _ = await _held_contest(setup, acme, clock, HELD)
    staff = await _staff(setup, acme)

    visitor = await _standings(setup, None)
    entrant = await _standings(setup, first)
    staffs = await _standings(setup, staff)

    # Others read entrant20 with only the submission made before 14:00,
    # and entrant21 with none, each told how many are held; a row with
    # nothing left reads no hidden value, so its total is the fallback.
    assert visitor.held_from == HOLD_FROM
    assert _rows_of(visitor) == [("entrant20", 1, 1), ("entrant21", 2, 1)]
    assert [cell.number for cell in visitor.rows[0].cells] == [1, None, 1]
    assert [cell.number for cell in visitor.rows[1].cells] == [0, 0, 0]
    # Their own row is whole to its entrant; the other is held.
    assert _rows_of(entrant) == [("entrant20", 1, 0), ("entrant21", 2, 1)]
    assert entrant.rows[0].cells[2] == BoardCell(
        "tries", Fraction(2), (Counted("islands", 1), Counted("islands", 2))
    )
    # Staff read every row whole.
    assert staffs.held_from is None
    assert [row.held for row in staffs.rows] == [0, 0]
    assert [(row.name, row.cells[2].number) for row in staffs.rows] == [
        ("entrant21", Fraction(1)),
        ("entrant20", Fraction(2)),
    ]


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
    assert await boards.end_hold(setup, manager, SPRING) is False
    state = await boards.hold_state(setup, manager, SPRING)
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
            await boards.end_hold(setup, refused, SPRING, confirmed=True)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    with pytest.raises(ConfirmationRequired) as asked:
        await boards.end_hold(setup, manager, SPRING)
    assert asked.value.extra == {"action": "end_hold"}
    assert [row.held for row in (await _standings(setup, None)).rows] == [1, 1]

    assert await boards.end_hold(setup, manager, SPRING, confirmed=True) is True
    ended = await _standings(setup, None)
    assert ended.held_from is None
    assert [row.held for row in ended.rows] == [0, 0]
    state = await boards.hold_state(setup, manager, SPRING)
    assert (state.from_, state.holding, state.ended_by, state.ended_by_name) == (
        HOLD_FROM,
        False,
        7,
        "ada",
    )
    assert state.ended_at == BEFORE_CLOSE
    # Ending it again answers the first end, which stays.
    assert await boards.end_hold(setup, manager, SPRING, confirmed=True) is False
    assert await _hold_ends(setup) == 1

    # A hold written again with a later `from` holds afresh.
    await write_contest(acme.fake, HELD.replace("14:00:00Z", "14:40:00Z"))
    again = await _standings(setup, None)
    assert again.held_from == at(14, 40)
    assert [row.held for row in again.rows] == [0, 1]
    assert (await boards.hold_state(setup, manager, SPRING)).holding

    await write_contest(acme.fake, OPEN)
    with pytest.raises(NotFound):
        await boards.end_hold(setup, manager, SPRING, confirmed=True)


async def test_the_contest_home_shows_an_entrant_their_own_row_of_each_board(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    first, second = await _held_contest(setup, acme, clock, HELD)

    home = await contest_home.home(setup, first, SPRING)
    staff_home = await contest_home.home(setup, await signed_in(setup, acme.fake, 7), SPRING)

    assert [(board.key, _rows_of(board)) for board in home.standing] == [
        ("standings", [("entrant20", 1, 0)]),
        ("gradebook", [("entrant20", None, 0)]),
    ]
    assert [cell.number for cell in home.standing[0].rows[0].cells] == [1, None, 2]
    assert staff_home.standing == ()
    # The other entrant's own row is whole to them, past the hold.
    other = await contest_home.home(setup, second, SPRING)
    assert [cell.number for cell in other.standing[0].rows[0].cells] == [1, None, 1]


RANKED = OPEN.replace("sort: [{sample: desc}, {tries: asc}]", "sort: [{total: desc}]", 1)
"""The boards, the one for everyone ranked by the total shown after the
close."""


async def _late_running(setup: Setup, row: Grading) -> None:
    """A late row of the early row `row`, still running."""
    async with setup.unit_of_work() as ctx:
        early = await ctx.db.get(Grading, row.id)
        assert early is not None
        ctx.db.add(
            Grading(
                task_id=early.task_id,
                workspace_id=early.workspace_id,
                submission_id=early.submission_id,
                submission_number=early.submission_number,
                submission_version=early.submission_version,
                submitted_at=early.submitted_at,
                publication_id=early.publication_id,
                pipeline="late",
                early_id=early.id,
                regrade_id=early.regrade_id,
                attempt=1,
                status=GradingStatus.RUNNING,
                queued_at=ctx.now,
            )
        )


async def test_a_value_whose_late_runs_still_run_after_the_reveal_waits_for_every_viewer(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=RANKED)
    sessions = []
    for user in (20, 21):
        acme.fake.add_user(user, f"entrant{user}")
        sessions.append(await _submit(setup, acme, islands, user=user))
    done, running = await _rows(setup, "early")
    await _ended(setup, done, ACCEPTED_EARLY, ACCEPTED_LATE)
    await _ended(setup, running, ACCEPTED_EARLY, None)
    await _late_running(setup, running)
    clock.set(AFTER_CLOSE)

    visitor = await _standings(setup, None)
    staff = await _standings(setup, await _staff(setup, acme))
    graded, grading = staff.rows
    own = await _standings(setup, sessions[[20, 21].index(int(grading.name[-2:]))])

    # The grading row's total still waits on its late runs, so the latest
    # pick passes over its submission and the total falls back to 0, as
    # staff read it; it is hidden from nobody, so the board keeps its
    # places.
    assert visitor.dropped == ()
    assert _rows_of(visitor) == [(graded.name, 1, 0), (grading.name, 2, 0)]
    assert [_cells(row) for row in visitor.rows] == [_cells(row) for row in staff.rows]
    assert _cells(visitor.rows[1])[1] == ("total", Fraction(0), False)
    # Only its own entrant, and staff, are told it waits.
    waits = (Counted("islands", running.submission_number),)
    assert grading.cells[1].waiting == waits
    assert own.rows[1].cells[1].waiting == waits
    assert not visitor.rows[1].told
    assert [row.grading for row in (own.rows[1], grading, visitor.rows[1])] == [1, 1, 0]


async def test_ending_a_hold_before_its_from_adds_nothing_and_it_still_holds_from_then(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(13, 0))
    islands = await _checked(setup, acme, contest=HELD)
    await _entrant(setup, acme, islands, 20)
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)

    # The hold has not started, so there is nothing to end: it answers as
    # it stands, asks nothing and adds no row.
    assert await boards.end_hold(setup, manager, SPRING) is False
    assert await _hold_ends(setup) == 0
    clock.set(at(14, 50))
    assert (await _standings(setup, None)).held_from == HOLD_FROM
    assert (await boards.hold_state(setup, manager, SPRING)).holding
