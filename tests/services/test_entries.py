"""Marks staff enter for a submission's `entered` values (SIMPLER-CORE.md
14.2, stage 5 section 6 items 8 and 9): a mark within its block's bounds
is kept and counts, one outside them is refused, exactly; a value is
missing until a mark is entered; every entry is kept and the latest
counts; a regrade never touches one; a manager of the task, a role at its
contest included, enters one and an observer is refused; a value the live
version gives no `entered` block takes none. The grading queue lists, for
each row once the task has closed for it, the submissions the boards'
cells counted with their marks, for an observer of the task, who enters
nothing; and an entry reaches its entrant only once a board cell shown to
them counted the submission, or, when no cell counted it, once the task
has revealed (#505, SIMPLER-CORE.md 14).
"""

import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import func, select, update

from forge.db.tables import Entry, Grading
from forge.db.tables.entrants import Entrant
from forge.domain.boards import BoardCell, Counted
from forge.domain.errors import Forbidden, InvalidMark, NotFound
from forge.domain.grading import GradingStatus
from forge.domain.names import EntrantOwner
from forge.domain.release import Extension
from forge.domain.roles import Role, Scope, contest_id_of, task_scope
from forge.domain.submissions import SubmittedInput
from forge.runtime.setup import Setup
from forge.services import boards, entries, gradings, marks, published, scores, submissions
from forge.testing import FakeClock, register_entrant
from tests.services.conftest import SPRING, Acme, organiser, signed_in, upload
from tests.services.test_pipelines import (
    ACCEPTED_EARLY,
    ACCEPTED_LATE,
    AFTER_CLOSE,
    BEFORE_CLOSE,
    ISLANDS,
    LIVE,
    _checked,
    _ended,
    _graded_both,
    _grading,
    _result,
    _rows,
    _values,
)
from tests.services.test_regrades import _held
from tests.services.test_run_task_saves import TASKS
from tests.services.test_steps import CODE, _save

REVIEWED = (
    TASKS["islands"][0]
    .replace(
        "    total:\n",
        "    review: {block: entered, at_least: 0, at_most: 10}\n"
        "    reviewed: {block: add, of: [{value: sample_score}, {value: review}]}\n"
        "    total:\n",
    )
    .replace(
        "now: [sample_score, sample_time, sample_tests, samples/]",
        "now: [sample_score, sample_time, sample_tests, samples/, review, reviewed]",
    )
)
"""islands with a reviewer's mark out of 10, and a value reading it, both
listed under `now`."""
LATER = REVIEWED.replace("samples/, review, reviewed]", "samples/]")
"""REVIEWED with the mark and what reads it listed under no time, so a
save may drop them."""
CONTEST = Scope("acme", "spring")


async def _owner(setup: Setup) -> EntrantOwner:
    async with setup.unit_of_work() as ctx:
        entrant = await ctx.db.scalar(select(Entrant.id))
    assert entrant is not None
    return EntrantOwner(entrant)


async def _organised(setup: Setup, acme: Acme, task: Any) -> dict[str, Any]:
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    seen = await submissions.organised(setup, manager, task, await _owner(setup), 1)
    assert seen.grading is not None
    return _values(seen.grading)


async def _enter(setup: Setup, acme: Acme, task: Any, mark: str, value: str = "review") -> Any:
    return await entries.enter(setup, acme.ada, task, await _owner(setup), 1, value, mark)


async def _entries(setup: Setup) -> list[tuple[str, str | None, int]]:
    async with setup.unit_of_work() as ctx:
        rows = await ctx.db.execute(
            select(Entry.value, Entry.mark, Entry.entered_by).order_by(Entry.seq)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def test_a_value_is_missing_until_a_mark_is_entered_and_the_latest_counts(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    before = await _organised(setup, acme, islands.task)
    assert (before["review"], before["reviewed"]) == (None, None)

    entered = await _enter(setup, acme, islands.task, "7.50")
    assert (entered.value, entered.mark, entered.by, entered.at) == (
        "review",
        "7.5",
        7,
        clock.now(),
    )
    after = await _organised(setup, acme, islands.task)
    assert (after["review"], after["reviewed"]) == (Fraction(15, 2), Fraction(17, 2))

    await _enter(setup, acme, islands.task, "10")
    assert (await _organised(setup, acme, islands.task))["review"] == 10
    assert await _entries(setup) == [("review", "7.5", 7), ("review", "10", 7)]


@pytest.mark.parametrize("mark", ["10.000001", "-0.5", "1e1", "seven", "", "NaN", "Infinity"])
async def test_a_mark_outside_its_bounds_or_not_a_plain_number_is_refused(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, mark: str
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(InvalidMark) as refused:
        await _enter(setup, acme, islands.task, mark)

    assert refused.value.detail == "Enter a number from 0 to 10 for review, such as 0."
    assert await _entries(setup) == []


@pytest.mark.parametrize("value", ["reviewed", "total", "nothing"])
async def test_a_value_the_live_version_gives_no_entered_block_takes_no_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, value: str
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(InvalidMark) as refused:
        await _enter(setup, acme, islands.task, "5", value=value)

    assert refused.value.detail == (
        f"{value} takes no entered mark: the task's live version gives it no entered block."
    )
    assert await _entries(setup) == []


async def test_a_manager_of_the_contest_enters_a_mark_and_an_observer_is_refused(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    owner = await _owner(setup)

    for scope in (ISLANDS, CONTEST):
        with pytest.raises(Forbidden):
            await entries.enter(
                setup, _held(acme, scope, Role.OBSERVER), islands.task, owner, 1, "review", "5"
            )
    manager = _held(acme, CONTEST, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, owner, 1, "review", "5")

    assert await _entries(setup) == [("review", "5", 9)]


async def test_a_submission_the_entrant_does_not_have_takes_no_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)

    with pytest.raises(NotFound):
        await entries.enter(setup, acme.ada, islands.task, await _owner(setup), 2, "review", "5")


async def test_an_entry_is_kept_through_a_regrade(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "4")

    await gradings.rejudge(setup, acme.ada, islands.task)
    await setup.settle()

    # Every item is reused from the store, so the new attempt has finished.
    assert [row.status for row in await _rows(setup, "early") if row.attempt == 2] == [
        GradingStatus.DONE
    ]
    assert (await _organised(setup, acme, islands.task))["review"] == 4
    assert await _entries(setup) == [("review", "4", 7)]
    async with setup.unit_of_work() as ctx:
        assert await ctx.db.scalar(select(func.count()).select_from(Entry)) == 1


async def test_an_entry_no_cell_counted_reaches_its_entrant_once_the_task_has_revealed(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, session = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "6")

    before = _values(await _grading(setup, session, islands))
    clock.set(AFTER_CLOSE)
    after = _values(await _grading(setup, session, islands))

    # The contest has no board, so no cell counted it: `show` alone, from
    # the reveal, reading as no mark before. Organisers read it at once.
    assert (before["review"], before["reviewed"]) == (None, None)
    assert (after["review"], after["reviewed"]) == (6, 7)
    assert {"review", "reviewed"} <= set(await _organised(setup, acme, islands.task))


async def test_a_mark_of_40_characters_is_kept_exactly_and_counts(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    tiny = "." + "0" * 38 + "1"
    many = "9." + "9" * 38

    kept = await _enter(setup, acme, islands.task, tiny)
    assert kept.mark == "0." + "0" * 38 + "1"
    assert (await _organised(setup, acme, islands.task))["review"] == Fraction(1, 10**39)
    assert (await _enter(setup, acme, islands.task, many)).mark == many
    assert (await _organised(setup, acme, islands.task))["review"] == Fraction(many)


async def test_a_kept_entry_that_is_no_mark_is_a_bug_not_a_missing_mark(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "5")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Entry).values(mark="five"))

    with pytest.raises(ValueError, match=r"An entry for review holds 'five', which is no mark\."):
        await _organised(setup, acme, islands.task)


async def test_a_value_removed_and_added_again_starts_with_no_marks(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, LATER)
    await _enter(setup, acme, islands.task, "4")
    unreviewed = TASKS["islands"][0]

    for text in (unreviewed, LATER):
        await _save(setup, acme, islands.task, {"task.yaml": text.encode()})
        await setup.settle()
        [*_, save] = await _rows(setup, "save")
        assert save.status == GradingStatus.DONE
        await setup.settle()

    assert (await _organised(setup, acme, islands.task))["review"] is None
    await _enter(setup, acme, islands.task, "6")
    assert (await _organised(setup, acme, islands.task))["review"] == 6
    # The old mark is kept, and a start before the new one.
    assert await _entries(setup) == [("review", "4", 7), ("review", None, 7), ("review", "6", 7)]


async def test_a_value_kept_through_a_save_keeps_its_marks(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, LATER)
    await _enter(setup, acme, islands.task, "4")

    await _save(
        setup,
        acme,
        islands.task,
        {"task.yaml": LATER.replace("at_most: 10", "at_most: 20").encode()},
    )
    await setup.settle()

    assert (await _organised(setup, acme, islands.task))["review"] == 4
    assert await _entries(setup) == [("review", "4", 7)]


async def test_the_shared_scoring_works_out_what_reads_a_mark_for_every_reader(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # An entrant's page works it out with no mark while what the boards'
    # cells counted holds the mark back (`submissions._runs`).
    clock.set(BEFORE_CLOSE)
    islands, _ = await _graded_both(setup, acme, REVIEWED)
    await _enter(setup, acme, islands.task, "6")
    [early] = [row for row in await _rows(setup, "early") if row.attempt == 1]

    async with setup.unit_of_work() as ctx:
        settings = await published.contest(ctx, contest_id_of(task_scope(islands.task)))
        current = await published.task(ctx, islands.task, settings)
        assert current is not None
        late = await scores.late_of(ctx, early)
        scored = await scores.run_scored(ctx, current, settings, Extension(), early, late)

    assert scored.whole.outcomes["review"].number == 6
    assert scored.early.outcomes["reviewed"].number == 7


GRADEBOOK = LIVE + (
    "leaderboards:\n"
    "  gradebook:\n"
    "    audience: {who: entrants, rows: own}\n"
    "    columns:\n"
    "      review:\n"
    "        block: otherwise\n"
    "        of:\n"
    "          - value:\n"
    "              - block: otherwise\n"
    "                of:\n"
    "                  - block: latest\n"
    "                    from: {results: islands}\n"
    "                    where: {value: [islands, marked]}\n"
    "                  - {block: latest, from: {results: islands}}\n"
    "              - review\n"
    "          - 0\n"
    "    sort: [{review: desc}]\n"
    "    display: [review]\n"
)
"""A gradebook each entrant reads alone, over islands' review of the
submission they marked, or of their latest when none is."""
BEST = LIVE + (
    "leaderboards:\n"
    "  best:\n"
    "    audience: {who: entrants, rows: own}\n"
    "    columns:\n"
    "      review:\n"
    "        value:\n"
    "          - {block: highest, from: {results: islands}, by: {value: [islands, total]}}\n"
    "          - review\n"
    "    display: [review]\n"
)
"""A board over islands' review of the submission with the highest total,
which is shown only after the close."""
END = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
WRONG_LATE = _result({"r3": "accepted", "r4": "wrong_answer"})
UNSHOWN = REVIEWED.replace("after_close: [st1, st2, total, all]", "after_close: [st1, st2, all]")
"""REVIEWED with a total no entrant is ever shown."""


async def _entrant(
    setup: Setup, acme: Acme, user: int, extension: timedelta = timedelta(0)
) -> tuple[Any, EntrantOwner]:
    """A new approved entrant signed in as `user`, named for it."""
    acme.fake.add_user(user, f"entrant{user}")
    entrant = await register_entrant(setup, SPRING, user, time_extension=extension)
    return await signed_in(setup, acme.fake, user), EntrantOwner(entrant)


async def _submitted(
    setup: Setup,
    acme: Acme,
    clock: FakeClock,
    islands: Any,
    session: Any,
    late: dict[str, Any] = ACCEPTED_LATE,
) -> None:
    """Another submission of islands by the entrant, a minute on, graded
    every early run accepted and the late ones as `late` gives them."""
    clock.advance(timedelta(minutes=1))
    made = await upload(setup, acme.fake, session, islands.task, CODE, input="code")
    await submissions.submit(
        setup,
        session,
        islands.task,
        {"code": SubmittedInput(uploads=(made.id,)), "language": SubmittedInput(value="python")},
        idempotency_key=uuid.uuid4().hex,
    )
    await setup.settle()
    for row in await _rows(setup, "early"):
        if row.status is not GradingStatus.DONE:
            await _ended(setup, row, ACCEPTED_EARLY, late)


def _seen(found: tuple[entries.QueueRow, ...]) -> list[tuple[str, list[tuple[int, Any]]]]:
    """Each row by name, with each submission's number and review mark."""
    return [
        (row.name, [(each.number, dict(each.marks)["review"]) for each in row.submissions])
        for row in found
    ]


async def _queued_contest(
    setup: Setup, acme: Acme, clock: FakeClock
) -> tuple[Any, Any, EntrantOwner, EntrantOwner]:
    """islands with a review mark, read by the gradebook; entrant20, who
    marked the first of two submissions, and entrant21, with ten minutes
    more, who marked none: islands; entrant20's session; both rows."""
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, GRADEBOOK)
    first, ana = await _entrant(setup, acme, 20)
    second, ben = await _entrant(setup, acme, 21, timedelta(minutes=10))
    for session in (first, first, second, second):
        await _submitted(setup, acme, clock, islands, session)
    await marks.mark(setup, first, islands.task, 1)
    return islands, first, ana, ben


async def test_the_queue_lists_each_rows_marked_or_latest_submission_once_closed_for_it(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands, _, ana, ben = await _queued_contest(setup, acme, clock)
    observer = await organiser(setup, acme.fake, 7, ISLANDS, Role.OBSERVER)

    clock.set(BEFORE_CLOSE)
    assert await entries.queue(setup, observer, islands.task) == ()
    clock.set(datetime(2026, 9, 26, 15, 5, tzinfo=UTC))
    [row] = await entries.queue(setup, observer, islands.task)
    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))
    both = await entries.queue(setup, observer, islands.task)

    # entrant21's close is ten minutes later, by their extension.
    assert row == entries.QueueRow(
        ana, "entrant20", END, (entries.Queued(1, (("gradebook", "review"),), (("review", None),)),)
    )
    assert _seen(both) == [("entrant20", [(1, None)]), ("entrant21", [(2, None)])]
    assert both[1].owner == ben and both[1].closed_at == END + timedelta(minutes=10)


async def test_a_mark_entered_from_the_queue_reaches_the_row_only_as_its_cell_does(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands, session, ana, _ = await _queued_contest(setup, acme, clock)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    clock.set(datetime(2026, 9, 26, 15, 5, tzinfo=UTC))

    [row] = await entries.queue(setup, manager, islands.task)
    [queued] = row.submissions
    await entries.enter(setup, manager, islands.task, row.owner, queued.number, "review", "7")

    assert _seen(await entries.queue(setup, manager, islands.task)) == [("entrant20", [(1, 7)])]
    # The task has not revealed, since entrant21's extension runs on: the
    # gradebook's cell counted the first submission, so its mark shows
    # there and on that submission, and nothing of the second's.
    [gradebook] = await boards.seen(setup, session, SPRING)
    assert [(each.column, each.number) for each in gradebook.rows[0].cells] == [
        ("review", Fraction(7))
    ]
    newest, oldest = await submissions.mine(setup, session, islands.task)
    assert oldest.grading is not None and newest.grading is not None
    assert (_values(oldest.grading)["review"], _values(oldest.grading)["reviewed"]) == (7, 8)
    assert (_values(newest.grading)["review"], _values(newest.grading)["reviewed"]) == (None, None)

    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))
    [newest, _] = await submissions.mine(setup, session, islands.task)
    assert newest.grading is not None and _values(newest.grading)["review"] is None
    assert ana == row.owner


async def test_an_entry_on_a_submission_chosen_by_a_hidden_value_shows_only_with_its_cell(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, BEST)
    session, owner = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    await _submitted(setup, acme, clock, islands, session, WRONG_LATE)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    # The first has the higher total, 100 to 40, which only staff see yet.
    await entries.enter(setup, manager, islands.task, owner, 1, "review", "7")
    clock.set(BEFORE_CLOSE)

    [board] = await boards.seen(setup, session, SPRING)
    seen = [
        _values(each.grading)
        for each in await submissions.mine(setup, session, islands.task)
        if each.grading
    ]
    assert board.rows[0].cells[0].hidden
    assert all((each["review"], each["reviewed"]) == (None, None) for each in seen)

    clock.set(AFTER_CLOSE)
    [board] = await boards.seen(setup, session, SPRING)
    newest, oldest = [
        _values(each.grading)
        for each in await submissions.mine(setup, session, islands.task)
        if each.grading
    ]
    assert board.rows[0].cells[0].number == 7
    assert (oldest["review"], newest["review"]) == (7, None)


async def test_an_entry_a_cell_never_shown_counted_stays_hidden_after_the_reveal(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, UNSHOWN, BEST)
    session, owner = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    await _submitted(setup, acme, clock, islands, session, WRONG_LATE)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, owner, 1, "review", "7")
    clock.set(AFTER_CLOSE)

    [board] = await boards.seen(setup, session, SPRING)
    newest, oldest = [
        _values(each.grading)
        for each in await submissions.mine(setup, session, islands.task)
        if each.grading
    ]

    # The board chose the first by a total no entrant is ever shown, so its
    # mark waits for that cell, and it reads as the second, which no cell
    # counted and has no mark.
    assert board.rows[0].cells[0].hidden
    assert (oldest["review"], oldest["reviewed"]) == (None, None)
    assert (newest["review"], newest["reviewed"]) == (None, None)


async def test_an_observer_of_the_task_reads_the_queue_and_cannot_enter(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands, _, ana, _ = await _queued_contest(setup, acme, clock)
    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))
    observer = _held(acme, ISLANDS, Role.OBSERVER)
    manager = _held(acme, ISLANDS, Role.MANAGER)

    assert len(await entries.queue(setup, observer, islands.task)) == 2
    with pytest.raises(Forbidden):
        await entries.enter(setup, observer, islands.task, ana, 1, "review", "5")
    assert len(await entries.queue(setup, manager, islands.task)) == 2
    await entries.enter(setup, manager, islands.task, ana, 1, "review", "5")
    with pytest.raises(Forbidden):
        await entries.queue(
            setup, _held(acme, Scope("acme", "spring", "sum"), Role.MANAGER), islands.task
        )

    assert await _entries(setup) == [("review", "5", 9)]


GIVEN = UNSHOWN.replace(
    "    total:\n",
    "    given: {block: if, test: {value: sample_score}, then: 1, else: {value: review}}\n"
    "    total:\n",
).replace("samples/, review, reviewed]", "samples/, review, reviewed, given]")
"""UNSHOWN with a value that reads the mark only when the sample score is
0, and is 1 otherwise."""
LATEST_BY_TOTAL = BEST.replace(
    "{block: highest, from: {results: islands}, by: {value: [islands, total]}}",
    "{block: latest, from: {results: islands}, where: {value: [islands, total]}}",
)
"""BEST over the latest submission with a total, not the highest."""


async def _page(setup: Setup, session: Any, islands: Any) -> dict[int, dict[str, Any]]:
    """The values of each of the entrant's graded submissions, by number."""
    return {
        each.number: _values(each.grading)
        for each in await submissions.mine(setup, session, islands.task)
        if each.grading
    }


@pytest.mark.parametrize(
    ("board", "chosen"), [(BEST, 1), (LATEST_BY_TOTAL, 2)], ids=["highest", "latest"]
)
async def test_a_mark_held_back_reads_as_no_mark_at_all(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, board: str, chosen: int
) -> None:
    # Each board chooses by a total no entrant is ever shown: BEST the
    # highest, the oldest of a tie (1); LATEST_BY_TOTAL the latest with a
    # total (2). Its mark held back, the chosen submission reads as the
    # other, which no cell counted and has no mark: the mark missing, and
    # `given`, which reads it only on a branch not taken, 1, so nothing
    # tells which one the board chose.
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, GIVEN, board)
    session, owner = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    await _submitted(setup, acme, clock, islands, session)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, owner, chosen, "review", "7")
    clock.set(AFTER_CLOSE)

    [seen] = await boards.seen(setup, session, SPRING)
    page = await _page(setup, session, islands)

    assert seen.rows[0].cells[0].hidden
    assert (page[chosen]["review"], page[chosen]["reviewed"], page[chosen]["given"]) == (
        None,
        None,
        1,
    )
    assert page[1] == page[2]


HELD_MARK = (
    LIVE + "hold: {from: 2026-09-26T14:00:00Z, until: staff}\n" + "leaderboards:\n"
    "  pub:\n"
    "    audience: {who: everyone}\n"
    "    columns:\n"
    "      review:\n"
    "        block: otherwise\n"
    "        of: [{value: [{block: latest, from: {results: islands}}, review]}, 0]\n"
    "    display: [review]\n"
    "  staff:\n"
    "    columns:\n"
    "      best:\n"
    "        value:\n"
    "          - {block: highest, from: {results: islands}, by: {value: [islands, total]}}\n"
    "          - review\n"
    "    display: [best]\n"
)
"""A board for everyone over the latest submission's review, held from
14:00 until staff end it, and a staff board over the review of the
submission with the highest total."""


async def test_the_hold_hides_a_mark_on_a_row_it_holds_and_a_staff_boards_count_holds_it_back(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, UNSHOWN, HELD_MARK)
    session, owner = await _entrant(setup, acme, 20)
    other, _ = await _entrant(setup, acme, 21)
    # 1 at 13:01, total 100; 2 at 14:31, after the hold's from, total 40.
    await _submitted(setup, acme, clock, islands, session)
    clock.set(datetime(2026, 9, 26, 14, 30, tzinfo=UTC))
    await _submitted(setup, acme, clock, islands, session, WRONG_LATE)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    clock.set(BEFORE_CLOSE)
    await entries.enter(setup, manager, islands.task, owner, 1, "review", "7")

    for when in (BEFORE_CLOSE, AFTER_CLOSE):
        clock.set(when)
        [visitor] = await boards.seen(setup, None, SPRING)
        [as_other] = await boards.seen(setup, other, SPRING)
        [own] = await boards.seen(setup, session, SPRING)
        page = await _page(setup, session, islands)

        # Item 117: the hold leaves only 1 on the row for anyone else, so
        # its mark would tell what the staff board chose; the cell is hidden,
        # as one reading `marked` is. The entrant's own row counts 2.
        for board in (visitor, as_other):
            [row] = [row for row in board.rows if row.owner == owner]
            assert row.cells[0] == BoardCell("review", None)
        [mine] = [row for row in own.rows if row.owner == owner]
        assert mine.cells[0] == BoardCell("review", Fraction(0), (Counted("islands", 2),))
        # Item 118: only the staff board counted 1, so its mark stays back,
        # past the reveal too, reading as no mark.
        assert (page[1]["review"], page[1]["reviewed"]) == (None, None)
    [queued] = await entries.queue(setup, manager, islands.task)
    assert [(each.number, each.counted_by) for each in queued.submissions] == [
        (1, (("staff", "best"),)),
        (2, (("pub", "review"),)),
    ]


THROUGH_PICK = LIVE + (
    "leaderboards:\n"
    "  pub:\n"
    "    audience: {who: entrants, rows: own}\n"
    "    columns:\n"
    "      last: {block: latest, from: {results: islands}}\n"
    "      review: {value: [last, review]}\n"
    "    display: [review]\n"
    "  staffs:\n"
    "    columns:\n"
    "      theirs: {column: [pub, last]}\n"
    "      best: {value: [theirs, total]}\n"
    "    display: [best]\n"
)
"""Each entrant's board over their latest submission's review, and a
staff board reading that pick's total, shown only after the close."""


async def test_a_board_the_entrant_never_reads_keeps_back_no_mark_their_own_cell_counted(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # Item 118: the entrant's own cells are read on the boards they read
    # alone, so the staff board's read of the total through their board's
    # pick hides nothing of it (item 98), and the mark of the submission
    # their cell counted shows before the reveal.
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, THROUGH_PICK)
    session, owner = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    await _submitted(setup, acme, clock, islands, session)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    await entries.enter(setup, manager, islands.task, owner, 2, "review", "7")
    clock.set(BEFORE_CLOSE)

    [board] = await boards.seen(setup, session, SPRING)
    page = await _page(setup, session, islands)

    assert board.rows[0].cells[0] == BoardCell("review", Fraction(7), (Counted("islands", 2),))
    assert (page[2]["review"], page[1]["review"]) == (7, None)


async def test_the_queue_lists_its_rows_by_name(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    islands, _, ana, ben = await _queued_contest(setup, acme, clock)
    # Named so that their names sort the other way from their ids.
    first, second = sorted((ana, ben), key=str)
    async with setup.unit_of_work() as ctx:
        for owner, name in ((first, "Zed"), (second, "Amy")):
            await ctx.db.execute(
                update(Entrant).where(Entrant.id == owner.entrant_id).values(name=name)
            )
    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))

    found = await entries.queue(setup, acme.ada, islands.task)

    assert [(row.owner, row.name) for row in found] == [(second, "Amy"), (first, "Zed")]


async def test_the_queue_of_a_task_never_published_is_not_found(
    setup: Setup, acme: Acme, sum_task: Any
) -> None:
    with pytest.raises(NotFound) as refused:
        await entries.queue(setup, acme.ada, sum_task)

    assert refused.value.detail == published.NO_SUCH_TASK


MIXED = GRADEBOOK + (
    "  mixed:\n"
    "    columns:\n"
    "      sums: {block: count, over: {results: sum}}\n"
    "      pick:\n"
    "        block: latest\n"
    "        from: {results: islands}\n"
    "        where: {block: ge, of: [{column: sums}, 0]}\n"
    "      review: {value: [pick, review]}\n"
    "    display: [review]\n"
)
"""The gradebook, and a staff board over the latest submission chosen
by a column reading the task sum."""


async def test_an_observer_of_the_task_alone_is_listed_only_the_cells_reading_it_alone(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # Item 119: which submission of islands `mixed` counts turns on sum's
    # results, so only a role at the contest is told that it counted one.
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, MIXED)
    session, _ = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    clock.set(datetime(2026, 9, 26, 15, 10, tzinfo=UTC))

    [by_task] = await entries.queue(setup, _held(acme, ISLANDS, Role.OBSERVER), islands.task)
    [by_contest] = await entries.queue(setup, _held(acme, CONTEST, Role.OBSERVER), islands.task)

    assert [(each.number, each.counted_by) for each in by_task.submissions] == [
        (1, (("gradebook", "review"),))
    ]
    assert [(each.number, each.counted_by) for each in by_contest.submissions] == [
        (1, (("gradebook", "review"), ("mixed", "review")))
    ]


async def test_the_queue_starts_no_retry_of_a_failure_it_finds(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Item 100: only staff's board read sees to a failure, so reading the
    # queue, which an observer of one task may, retries nothing on any task.
    monkeypatch.setattr(setup.settings, "retry_failed", True)
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, GRADEBOOK)
    session, _ = await _entrant(setup, acme, 20)
    await _submitted(setup, acme, clock, islands, session)
    [first] = await _rows(setup, "early")
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Grading, first.id)
        assert row is not None
        row.status, row.error = GradingStatus.SYSTEM_ERROR, "The step crashed."

    await entries.queue(setup, _held(acme, ISLANDS, Role.OBSERVER), islands.task)
    await setup.settle()

    assert [(row.attempt, row.automatic) for row in await _rows(setup, "early")] == [(1, False)]


SUMMED = LIVE + (
    "leaderboards:\n"
    "  reviews:\n"
    "    audience: {who: entrants, rows: own}\n"
    "    columns:\n"
    "      reviewed: {block: sum, over: {results: islands}, of: {value: [islands, review]}}\n"
    "    display: [reviewed]\n"
)
"""A leaderboard each entrant reads alone, adding up the review of every
submission of islands."""


async def test_an_unmarked_submission_keeps_a_sum_missing_and_is_queued_until_marked(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # Item 127: one missing state passes through, so a sum of marks is
    # missing while one is still to come, and counts that submission, so
    # the queue lists it for marking.
    clock.set(datetime(2026, 9, 26, 13, 0, tzinfo=UTC))
    islands = await _checked(setup, acme, REVIEWED, SUMMED)
    session, owner = await _entrant(setup, acme, 20)
    for _ in range(2):
        await _submitted(setup, acme, clock, islands, session)
    manager = await organiser(setup, acme.fake, 7, ISLANDS, Role.MANAGER)
    clock.set(datetime(2026, 9, 26, 15, 5, tzinfo=UTC))

    async def reviews() -> BoardCell:
        [board] = await boards.seen(setup, session, SPRING)
        return board.rows[0].cells[0]

    await entries.enter(setup, manager, islands.task, owner, 1, "review", "4")
    [row] = await entries.queue(setup, manager, islands.task)
    missing = await reviews()
    await entries.enter(setup, manager, islands.task, owner, 2, "review", "5")
    summed = await reviews()

    assert _seen((row,)) == [("entrant20", [(1, 4), (2, None)])]
    assert (missing.number, missing.counted) == (
        None,
        (Counted("islands", 1), Counted("islands", 2)),
    )
    assert summed.number == 9
