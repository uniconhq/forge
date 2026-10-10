"""The contest's boards, read by a visitor, a contestant and an organiser,
each given what their audience sees: one ranking, the viewer's own row the
only one with its grading count and counted submissions, organisers every
row `now` and `final`, and a picked row as it sees `now`. A broken attempt
counts as still grading, or void once cancelled, unless a fallback, the
contest's or staff's, counts its last good result. A row's marks:
up to the task's `marks` of its own submissions, frozen at its close. What
a board asks of its tasks, refused on a task's save (T8) and on the
contest's (C4), with the save's report of the boards it moves (T9); and a
task's `marks` never lowered below what a row holds (C1).
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import select, update

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import (
    Forbidden,
    MarkLimit,
    MarksFrozen,
    MarksOff,
    NotApproved,
    NotFound,
)
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM, User
from forge.domain.ids import TaskId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.domain.showing import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import (
    boards,
    contest_home,
    contestants,
    files,
    gradings,
    identity,
    marks,
    publications,
    submissions,
)
from forge.services.access import Organiser
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


async def _contestant(setup: Setup, acme: Acme, user_id: int, name: str) -> Session:
    acme.fake.add_user(user_id, name)
    session = await signed_in(setup, acme.fake, user_id)
    await contestants.register(setup, session, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, user_id)
    return session


async def test_an_organiser_picks_a_row_of_the_contest_and_reads_the_boards_it_sees_as_it_does(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS + "  - {name: Staff, who: organisers}\n"))
    observer = await organiser(setup, acme.fake, 7, Scope("acme", "spring"))
    await _contestant(setup, acme, 30, "cyd")

    with pytest.raises(NotFound):
        await boards.organised(setup, observer, SPRING, row=UserOwner(31))
    picked = await boards.organised(setup, observer, SPRING, row=UserOwner(30))

    assert [each.now.board.name for each in picked] == ["Standings", "Final"]
    bob = next(row for row in picked[0].now.rows if row.name == "bob")
    assert bob.cells["sum"].submissions is None
    cyd = next(row for row in picked[0].now.rows if row.name == "cyd")
    assert cyd.cells["sum"].submissions == ()


def _main(outcome: str = "accepted", **values: Any) -> dict[str, Any]:
    return {**RESULT, "tests": [{"test": "main/1", "outcome": outcome, "values": values}]}


async def _grade_waiting(setup: Setup, result: dict[str, Any]) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).where(Grading.status != "done").values(status="done", result=result)
        )


async def test_a_compile_error_from_a_step_that_is_not_sealed_is_no_attempt(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await write_contest(acme.fake, contest(BOARDS))
    made = [
        ({**RESULT, "stopped": "compile_error", "stopped_by": "compile", "tests": []}),
        _main("wrong_answer"),
        _main(),
    ]
    for index, result in enumerate(made):
        await _submit(setup, acme, entered, f"key-000{index}-attempts")
        await _grade_waiting(setup, result)
        clock.advance(timedelta(minutes=1))

    standings, _ = await boards.seen(setup, entered.session, SPRING)

    (bob,) = standings.rows
    assert bob.keys[0] == 100
    assert (bob.cells["sum"].attempts, bob.cells["sum"].submissions) == (1, (3,))


async def test_a_run_in_system_error_is_still_grading_to_its_row_and_no_attempt(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS))
    await _submit(setup, acme, entered, "key-0001-fault")
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(status="system_error", error="It crashed."))

    standings, _ = await boards.seen(setup, entered.session, SPRING)

    cell = standings.rows[0].cells["sum"]
    assert (cell.counting, cell.attempts, cell.grading) == (False, 0, 1)


async def _manager(setup: Setup, acme: Acme) -> Organiser:
    return await organiser(setup, acme.fake, 7, Scope("acme", "spring", "sum"), Role.MANAGER)


async def _broken_retry(setup: Setup, manager: Organiser, key: str) -> uuid.UUID:
    """The submission made with `key` retried, and the retry ended in a
    system error: the retry's id.
    """
    async with setup.unit_of_work() as ctx:
        first = await ctx.db.scalar(
            select(Grading.id).where(Grading.idempotency_key == key, Grading.attempt == 1)
        )
    assert first is not None
    second = (await gradings.retry(setup, manager, first)).id
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.id == second)
            .values(status="system_error", error="It crashed.")
        )
    return second


async def test_a_fallback_counts_the_last_good_result_of_a_broken_attempt_a_cancel_included(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS))
    await _submit(setup, acme, entered, "key-0001-fault")
    await _grade_waiting(setup, _main())
    manager = await _manager(setup, acme)
    second = await _broken_retry(setup, manager, "key-0001-fault")

    async def cell() -> tuple[Fraction | None, bool, int | None]:
        standings, _ = await boards.seen(setup, entered.session, SPRING)
        (bob,) = standings.rows
        found = bob.cells["sum"]
        return bob.keys[0], found.counting, found.grading

    assert (await cell())[1:] == (False, 1)
    await write_contest(acme.fake, contest(BOARDS + "on_system_error: last_result\n"))
    assert await cell() == (Fraction(100), True, 0)
    await gradings.cancel(setup, manager, second, "It crashes on every try.")
    assert await cell() == (Fraction(100), True, 0)
    await write_contest(acme.fake, contest(BOARDS))
    assert (await cell())[1:] == (False, 0)
    await gradings.fall_back(setup, manager, second)
    assert await cell() == (Fraction(100), True, 0)
    await gradings.clear_fallback(setup, manager, second)
    assert (await cell())[1:] == (False, 0)


async def test_relative_credit_is_against_the_best_of_the_rows_candidates_only(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS))
    current = await _task_yaml(acme, entered.task)
    saved = await _save(
        setup, acme, entered.task, {"task.yaml": current + b"credit: {relative: time_ms}\n"}
    )
    assert isinstance(saved, Published), saved
    cyd = await _contestant(setup, acme, 30, "cyd")
    await _submit(setup, acme, entered, "key-0001-bob")
    await _grade_waiting(setup, _main(time_ms=10))
    await _submit(setup, acme, Entered(cyd, entered.task), "key-0001-cyd")
    await _grade_waiting(setup, _main(time_ms=20))

    def points(standings: Any) -> dict[str, Fraction | None]:
        return {row.name: row.keys[0] for row in standings.rows}

    (before,) = await boards.seen(setup, None, SPRING)
    async with setup.unit_of_work() as ctx:
        first = await ctx.db.scalar(
            select(Grading).where(Grading.idempotency_key == "key-0001-bob")
        )
        assert first is not None
        ctx.db.add(
            Grading(
                task_id=first.task_id,
                workspace_id=first.workspace_id,
                submission_id=first.submission_id,
                submission_number=first.submission_number,
                submission_version=first.submission_version,
                submitted_at=first.submitted_at,
                publication_id=first.publication_id,
                attempt=2,
                status="cancelled",
                cancel_reason="Copied.",
            )
        )
    (after,) = await boards.seen(setup, None, SPRING)

    assert points(before) == {"bob": 100, "cyd": 50}
    assert points(after) == {"cyd": 100, "bob": 0}


async def test_relative_credit_counts_a_fallen_back_result_while_the_fallback_holds(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest(BOARDS))
    current = await _task_yaml(acme, entered.task)
    saved = await _save(
        setup, acme, entered.task, {"task.yaml": current + b"credit: {relative: time_ms}\n"}
    )
    assert isinstance(saved, Published), saved
    cyd = await _contestant(setup, acme, 30, "cyd")
    await _submit(setup, acme, entered, "key-0001-bob")
    await _grade_waiting(setup, _main(time_ms=10))
    await _submit(setup, acme, Entered(cyd, entered.task), "key-0001-cyd")
    await _grade_waiting(setup, _main(time_ms=20))
    manager = await _manager(setup, acme)
    broken = await _broken_retry(setup, manager, "key-0001-bob")

    async def points() -> dict[str, Fraction | None]:
        (standings,) = await boards.seen(setup, None, SPRING)
        return {row.name: row.keys[0] for row in standings.rows}

    assert await points() == {"cyd": 100, "bob": 0}
    await gradings.fall_back(setup, manager, broken)
    assert await points() == {"bob": 100, "cyd": 50}
    await gradings.clear_fallback(setup, manager, broken)
    assert await points() == {"cyd": 100, "bob": 0}


async def test_a_fallen_back_result_shows_only_what_is_shown_and_counts_its_mark_at_the_reveal(
    setup: Setup, acme: Acme, ranked: Entered, clock: FakeClock
) -> None:
    await _broken_retry(setup, await _manager(setup, acme), "key-0002-bbbb")
    await marks.mark(setup, ranked.session, ranked.task, 2)
    await write_contest(acme.fake, contest(BOARDS + "on_system_error: last_result\n"))

    standings, final = await boards.seen(setup, ranked.session, SPRING)
    told = (await submissions.one(setup, ranked.session, ranked.task, 2)).grading

    # Before the reveal the second's large group is held back, as it was
    # before its retry broke: 65 shown and 35 pending, and nothing of Final.
    assert standings.rows[0].keys == keys(65, 120)
    assert final.nothing_shown
    assert told is not None and (told.attempt, told.status) == (1, SubmissionState.GRADED)
    assert told.points is not None
    assert (told.points.shown, told.points.pending) == keys(65, 35)
    assert [group.outcome for group in told.groups if group.group == "large"] == [None]

    clock.set(END)
    await identity.current(ranked.session.id, setup=setup)
    _, final = await boards.seen(setup, ranked.session, SPRING)
    assert (final.rows[0].keys, final.rows[0].cells["sum"].submissions) == (keys(35), (2,))
    await write_contest(acme.fake, contest(BOARDS))
    _, final = await boards.seen(setup, ranked.session, SPRING)
    assert final.rows[0].keys == keys(0)


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
    assert (await contest_home.task(setup, entered.session, entered.task)).marks is None

    await write_contest(acme.fake, contest(BOARDS))
    assert (await contest_home.task(setup, entered.session, entered.task)).marks == 1


async def test_someone_not_approved_is_not_told_whether_a_task_takes_marks(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    for settings in (contest(), contest(BOARDS)):
        await write_contest(acme.fake, settings)
        with pytest.raises(NotApproved):
            await marks.held(setup, cyd, entered.task)
        assert (await contest_home.task(setup, cyd, entered.task)).marks is None


async def test_another_persons_submission_is_not_theirs_to_mark(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    cyd = await _contestant(setup, acme, 30, "cyd")

    with pytest.raises(NotFound):
        await marks.mark(setup, cyd, ranked.task, 1)


async def _write(setup: Setup, acme: Acme, content: str) -> files.Written:
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    written = await files.write(
        setup, acme.ada, SPRING, "contest.yaml", content.encode(), current.token
    )
    assert isinstance(written, files.Written)
    return written


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


async def test_the_contests_save_answers_each_board_that_counts_nothing_from_a_task(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    current = await _task_yaml(acme, entered.task)
    pointless = current[: current.index(b"test_groups:")] + b"test_groups:\n  main: {}\n"
    assert isinstance(await _save(setup, acme, entered.task, {"task.yaml": pointless}), Published)

    written = await _write(setup, acme, contest(BOARDS))

    assert written.notes == (
        "Standings counts nothing from sum.",
        "Final counts nothing from sum.",
    )
    (standings, final) = await boards.organised(setup, acme.ada, SPRING)
    assert standings.notes + final.notes == written.notes


async def test_the_contests_save_with_nothing_to_report_answers_no_notes(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    counting = "leaderboards:\n  - {name: Standings, who: everyone, order: [points, penalty]}\n"
    written = await _write(setup, acme, contest(counting))
    plain = await _write(setup, acme, contest())

    assert written.notes == plain.notes == ()
    assert written.version != plain.version


# The organisers' scored view of a submission (TASK-FORMAT.md section 1.7)


def _observer_of(acme: Acme, scope: Scope) -> Organiser:
    """Someone checked as an observer at `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, Role.OBSERVER),),
        scope=scope,
        role=Role.OBSERVER,
        identity=acme.ada.identity,
    )


async def test_an_organiser_reads_a_hidden_groups_points_and_tests_before_the_reveal(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    observer = _observer_of(acme, Scope("acme", "spring", "sum"))

    seen = await submissions.organised(setup, observer, ranked.task, UserOwner(8), 1)
    own = await submissions.one(setup, ranked.session, ranked.task, 1)

    assert seen.grading is not None and own.grading is not None
    assert seen.grading.status is GradingStatus.DONE
    assert own.grading.status is SubmissionState.GRADED
    by_group = {group.group: group for group in seen.grading.groups}
    large = by_group["large"]
    assert (large.outcome, large.points, large.max) == ("wrong_answer", Fraction(0), Fraction(35))
    assert large.tests is not None and [test["test"] for test in large.tests] == ["large/1"]
    assert large.shown_at == END
    small = by_group["small"]
    assert small.tests is not None and small.shown_at == END
    assert by_group["main"].shown_at is None
    assert seen.grading.outcome == "wrong_answer"
    assert seen.grading.points is not None
    assert (seen.grading.points.shown, seen.grading.points.pending) == (Fraction(65), Fraction(0))
    assert seen.grading.folded["time_ms"] == Fraction(30)
    assert (seen.number, seen.submitted_at, seen.late_days) == (
        own.number,
        own.submitted_at,
        own.late_days,
    )


async def test_a_contestants_own_read_is_unchanged_by_an_organisers(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    before = await submissions.one(setup, ranked.session, ranked.task, 1)

    await submissions.organised(
        setup, _observer_of(acme, Scope("acme", "spring")), ranked.task, UserOwner(8), 1
    )
    after = await submissions.one(setup, ranked.session, ranked.task, 1)

    assert after == before
    assert before.grading is not None
    large = next(group for group in before.grading.groups if group.group == "large")
    assert (large.outcome, large.tests, large.points, large.shown_at) == (None, None, None, END)
    assert before.grading.points is not None
    assert before.grading.points.pending == Fraction(35)


async def test_an_organiser_without_the_observer_role_at_the_task_is_refused(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    elsewhere = _observer_of(acme, Scope("acme", "spring", "product"))

    with pytest.raises(Forbidden):
        await submissions.organised(setup, elsewhere, ranked.task, UserOwner(8), 1)
    with pytest.raises(Forbidden):
        await gradings.list(setup, elsewhere, ranked.task)


async def test_a_row_with_no_such_submission_is_no_such_submission(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    observer = _observer_of(acme, Scope("acme", "spring"))

    for owner, number in ((UserOwner(8), 3), (UserOwner(7), 1)):
        with pytest.raises(NotFound):
            await submissions.organised(setup, observer, ranked.task, owner, number)
