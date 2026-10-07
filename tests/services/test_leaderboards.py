"""The contest's boards, read by a visitor, a contestant and an organiser,
each given what their audience sees: one ranking, the viewer's own row the
only one with its grading count and counted submissions, organisers every
row `now` and `final`, and a picked row as it sees `now`. A row's marks:
up to the task's `marks` of its own submissions, frozen at its close. What
a board asks of its tasks, refused on a task's save (T8) and on the
contest's (C4), with the save's report of the boards it moves (T9); and a
task's `marks` never lowered below what a row holds (C1).
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import update

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import MarkLimit, MarksFrozen, MarksOff, NotFound
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId, VersionId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import (
    boards,
    contestants,
    files,
    identity,
    marks,
    publications,
    submissions,
)
from forge.services.publications import Draft, Published
from forge.testing import FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    make_task,
    organiser,
    publish,
    signed_in,
    upload,
    write_contest,
)

END = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

BOARDS = """\
leaderboards:
  - {name: Standings, who: everyone, order: [points, penalty]}
  - {name: Final, who: contestants, over: after_close, select: marked}
"""

GROUPS = b"""\
test_groups:
  main: {each: 100}
  small: {pass: 30, show: verdict}
  large: {pass: 70, show: after_close}
"""

RESULT: dict[str, Any] = {
    "schema_version": 5,
    "stopped": None,
    "stopped_by": None,
    "tests": [
        {"test": "large/1", "outcome": "wrong_answer", "values": {"time_ms": 30, "memory_kb": 9}},
        {"test": "main/1", "outcome": "accepted", "values": {"time_ms": 10, "memory_kb": 7}},
        {"test": "small/1", "outcome": "accepted", "values": {"time_ms": 20, "memory_kb": 8}},
    ],
    "values": {},
    "run_log": None,
    "error": None,
}

EVERY_TEST: dict[str, Any] = {
    **RESULT,
    "tests": [{**test, "outcome": "accepted"} for test in RESULT["tests"]],
}


def keys(*numbers: int) -> tuple[Fraction | None, ...]:
    return tuple(Fraction(number) for number in numbers)


def contest(extra: str = "", entry: str = "") -> str:
    return RUNNING.format(visibility="everyone") + entry + extra


async def _task_yaml(acme: Acme, task: TaskId) -> bytes:
    return (await acme.fake.content.read_file(PLATFORM, task, "task.yaml")).content


async def _save(
    setup: Setup, acme: Acme, task: TaskId, files_: Mapping[str, bytes]
) -> Published | Draft:
    head = await acme.fake.content.list_files(PLATFORM, task)
    return await publications.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens.get(path)) for path, content in files_.items()},
        confirm=True,
    )


async def _three_groups(setup: Setup, acme: Acme, task: TaskId) -> Published:
    current = await _task_yaml(acme, task)
    start = current.index(b"test_groups:")
    changed = {"task.yaml": current[:start] + GROUPS}
    for group in ("small", "large"):
        changed[f"tests/{group}/1/input"] = b"1 1\n"
        changed[f"tests/{group}/1/answer"] = b"2\n"
    saved = await _save(setup, acme, task, changed)
    assert isinstance(saved, Published), saved
    return saved


async def _submit(setup: Setup, acme: Acme, entered: Entered, key: str) -> int:
    made = await upload(setup, acme.fake, entered.session, entered.task, key.encode())
    submission = await submissions.submit(
        setup,
        entered.session,
        entered.task,
        {
            "submission": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    return submission.number


async def _graded(setup: Setup, number: int, result: dict[str, Any]) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.submission_number == number)
            .values(status="done", result=result)
        )


@pytest.fixture
async def ranked(setup: Setup, acme: Acme, entered: Entered, clock: FakeClock) -> Entered:
    """bob's two graded submissions to sum, under both boards: the first,
    at 12:00, passes main and small; the second, at 12:01, everything.
    """
    await write_contest(acme.fake, contest(BOARDS))
    await _three_groups(setup, acme, entered.task)
    first = await _submit(setup, acme, entered, "key-0001-aaaa")
    await _graded(setup, first, RESULT)
    clock.advance(timedelta(minutes=1))
    second = await _submit(setup, acme, entered, "key-0002-bbbb")
    await _graded(setup, second, EVERY_TEST)
    return entered


async def test_a_visitor_reads_only_the_board_for_everyone_with_no_counts_of_any_row(
    setup: Setup, ranked: Entered
) -> None:
    (standings,) = await boards.seen(setup, None, SPRING)

    assert standings.board.name == "Standings"
    (bob,) = standings.rows
    assert (bob.name, bob.rank, bob.keys) == ("bob", 1, keys(65, 120))
    assert (bob.cells["sum"].grading, bob.cells["sum"].submissions) == (None, None)
    assert [(each.task, each.groups) for each in standings.not_in_view] == [("sum", ("large",))]


async def test_a_contestant_reads_their_own_row_with_its_counts_and_the_hidden_board_waits(
    setup: Setup, ranked: Entered
) -> None:
    standings, final = await boards.seen(setup, ranked.session, SPRING)

    (bob,) = standings.rows
    assert bob.owner == UserOwner(8)
    assert (bob.cells["sum"].grading, bob.cells["sum"].submissions) == (0, (1,))
    assert final.nothing_shown
    assert final.shown_at == END
    assert final.rows == ()


async def test_after_the_reveal_the_marked_board_counts_the_mark_or_the_best_seen_before(
    setup: Setup, acme: Acme, ranked: Entered, clock: FakeClock
) -> None:
    clock.set(END)
    await identity.current(ranked.session.id, setup=setup)

    standings, final = await boards.seen(setup, ranked.session, SPRING)

    assert standings.rows[0].keys == keys(100, 121)
    assert standings.rows[0].cells["sum"].submissions == (2,)
    # bob marked nothing: what he saw before the reveal put the first and
    # the second at 65, so the earlier, the first, counts, and its large
    # failed.
    assert (final.rows[0].keys, final.rows[0].cells["sum"].counting) == (keys(0), False)


async def test_an_organiser_reads_now_every_row_and_final_as_every_task_revealed(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"))

    standings, final = await boards.organised(setup, observer, SPRING)

    assert standings.now.rows[0].keys == keys(65, 120)
    assert standings.now.rows[0].cells["sum"].submissions == (1,)
    assert standings.final.rows[0].keys == keys(100, 121)
    assert final.now.nothing_shown
    assert final.final.rows[0].keys == keys(0)
    assert standings.notes == final.notes == ()
    picked = await boards.organised(setup, observer, SPRING, row=UserOwner(9))
    assert picked[0].now.rows[0].cells["sum"].submissions is None


async def test_a_mark_holds_up_to_the_tasks_marks_and_freezes_at_the_close(
    setup: Setup, acme: Acme, ranked: Entered, clock: FakeClock
) -> None:
    empty = await marks.held(setup, ranked.session, ranked.task)
    assert (empty.numbers, empty.most, empty.closes_at, empty.frozen) == ((), 1, END, False)

    marked = await marks.mark(setup, ranked.session, ranked.task, 2)
    again = await marks.mark(setup, ranked.session, ranked.task, 2)
    with pytest.raises(MarkLimit) as limit:
        await marks.mark(setup, ranked.session, ranked.task, 1)
    with pytest.raises(NotFound):
        await marks.mark(setup, ranked.session, ranked.task, 7)

    assert marked.numbers == again.numbers == (2,)
    assert limit.value.extra["limit"] == 1
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"))
    _, final = await boards.organised(setup, observer, SPRING)
    assert final.final.rows[0].keys == keys(35)

    unmarked = await marks.unmark(setup, ranked.session, ranked.task, 2)
    assert unmarked.numbers == ()
    await marks.mark(setup, ranked.session, ranked.task, 1)
    clock.set(END)
    await identity.current(ranked.session.id, setup=setup)
    with pytest.raises(MarksFrozen):
        await marks.unmark(setup, ranked.session, ranked.task, 1)
    assert (await marks.held(setup, ranked.session, ranked.task)).frozen


async def test_a_task_no_marked_board_covers_takes_no_marks(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest())
    with pytest.raises(MarksOff):
        await marks.held(setup, entered.session, entered.task)


async def test_another_persons_submission_is_not_theirs_to_mark(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    await contestants.register(setup, cyd, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, 30)

    with pytest.raises(NotFound):
        await marks.mark(setup, cyd, ranked.task, 1)


async def _write(setup: Setup, acme: Acme, content: str) -> VersionId:
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    written = await files.write(
        setup, acme.ada, SPRING, "contest.yaml", content.encode(), current.token
    )
    assert isinstance(written, str)
    return VersionId(written)


async def _refused(setup: Setup, acme: Acme, content: str) -> list[tuple[str, str]]:
    with pytest.raises(InvalidDefinition) as refused:
        await _write(setup, acme, content)
    return [(problem["path"], problem["message"]) for problem in refused.value.errors]


async def test_a_tasks_marks_are_not_lowered_below_what_a_row_holds(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS, "    marks: 2\n"))
    await marks.mark(setup, ranked.session, ranked.task, 1)
    await marks.mark(setup, ranked.session, ranked.task, 2)

    refused = await _refused(setup, acme, contest(BOARDS, "    marks: 1\n"))
    dropped = await _refused(setup, acme, contest(BOARDS))

    said = "1 row holds 2 marks on sum; it cannot take fewer."
    assert refused == [("tasks[0].marks", said)]
    assert dropped == [("tasks[0]", said)]
    assert await _write(setup, acme, contest(BOARDS, "    marks: 3\n"))


async def test_a_lower_is_better_value_first_over_two_tasks_is_refused_on_the_contests_save(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    product = await make_task(setup, acme, "product")
    await write_contest(acme.fake, contest(entry="  - id: product\n"))
    await publish(setup, acme, product)
    fastest = "leaderboards:\n  - {name: Fastest, order: [time_ms]}\n"

    refused = await _refused(setup, acme, contest(fastest, "  - id: product\n"))
    alone = "leaderboards:\n  - {name: Fastest, tasks: [sum], order: [time_ms]}\n"

    said = (
        "Fastest ranks time_ms, which is lower is better, over more than one task: rank it on "
        "a board of one task."
    )
    assert refused == [("leaderboards[0].order[0]", said), ("leaderboards[0].order[0]", said)]
    assert await _write(setup, acme, contest(alone, "  - id: product\n"))


async def test_a_tasks_save_is_refused_where_a_board_ranks_what_its_groups_hide(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(
        acme.fake, contest("leaderboards:\n  - {name: Fast, over: live, order: [time_ms]}\n")
    )
    current = await _task_yaml(acme, entered.task)
    hidden = current.replace(b"main: {each: 100}", b"main: {pass: 100, show: verdict}")

    refused = await _save(setup, acme, entered.task, {"task.yaml": hidden})

    assert isinstance(refused, Draft)
    assert [(error["path"], error["message"]) for error in refused.errors] == [
        (
            "test_groups",
            "Fast ranks time_ms over live, so sum needs a group shown always, since a verdict "
            "group's tests show only at the reveal.",
        )
    ]


async def test_a_tasks_save_reports_each_groups_most_its_reveal_and_the_boards_it_moves(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS))

    saved = await _three_groups(setup, acme, entered.task)
    current = await _task_yaml(acme, entered.task)
    pointless = await _save(
        setup,
        acme,
        entered.task,
        {
            "task.yaml": current[: current.index(b"test_groups:")]
            + b"test_groups:\n  main: {}\n  small: {}\n  large: {show: after_close}\n"
        },
    )

    assert saved.notes[-4:] == (
        "Each group's most points: main 50, small 15, large 35.",
        "sum reveals at 2026-09-26T15:00:00+00:00.",
        "This save moves Standings.",
        "This save moves Final.",
    )
    assert isinstance(pointless, Published), pointless
    assert "Standings counts nothing from sum." in pointless.notes
    assert "Final counts nothing from sum." in pointless.notes
