"""The examples of SIMPLER-CORE.md 9 and 10 and TASK-FORMAT.md 3.7, written
from the blocks as the leaderboards note's 5.8 does, saved and scored end
to end against the fake CI with result fixtures (stage 5, phase 5.9): the
note's 5.9, Ana and Ben over ICPC's `a` and `b` with the hold from 4:00;
islands with its checks, and the olympiad's `results` with `best_each`, a
column reading a value not shown yet hidden before the reveal; cartpole with
the course's `gradebook` and `cartpole_ranking`, the `search` mark entered
from the grading queue and reaching its row only as its cell does;
Kaggle's `house-prices`, one axis over its public and private split, each
split's RMSE reported by the checker, ranked lowest first on two boards,
a malformed submission ranking nowhere while the rest still count; and a
course keeping the best two of three homework with `sum_best`, beside a
final that takes one submission.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

import pytest

from forge.domain.boards import Board, BoardRow
from forge.domain.errors import SubmissionLimit
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.workflows import Visibility
from forge.runtime.setup import Setup
from forge.services import boards, entries, marks, submissions
from forge.testing import FakeClock, register_entrant
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    make_task,
    organiser,
    signed_in,
    upload,
    write_contest,
)
from tests.services.test_pipelines import (
    CHECK_FILES,
    CHECKS,
    _checks_run,
    _ended,
    _rows,
    _staff,
)
from tests.services.test_run_task_saves import ISLANDS_DATA, TASKS, _primitives, checked
from tests.services.test_steps import CODE, _report, _save
from tests.test_boards import GRADEBOOK, ICPC, OLYMPIAD
from tests.test_run_task import CARTPOLE_FILES

CONTEST = Scope("acme", "spring")
BASE = RUNNING.format(visibility="everyone").replace("tasks:\n  - id: sum\n", "")
"""acme/spring, running from 10:00 to 15:00 for everyone, with no task."""
ISLANDS_TEXT = TASKS["islands"][0]

KAGGLE = """\
inputs:
  predictions: {type: file, title: Predictions}
  answers: file
steps:
  - id: scorer
    use: unicon/compile@v3
    with: {source: "${{ workflow.scorer/ }}", language: python}
  - id: score
    use: unicon/exec@v1
    with:
      program: ${{ steps.scorer.program }}
      manifest: ${{ workflow.scorer/program.yaml }}
      files: {predictions: "${{ inputs.predictions }}", answers: "${{ inputs.answers }}"}
      time_limit: 60
      memory_limit: 1024
reports:
  rmse: ${{ steps.score.rmse }}
"""
"""A prediction task's run workflow: the organiser's scorer reads the
entrant's predictions against one split's answers and reports their RMSE,
lower being better, with no worst bound."""
KAGGLE_FILES = {
    "workflow.yaml": KAGGLE.encode(),
    "scorer/score.py": b"print(0.1)\n",
    "scorer/program.yaml": b"reports:\n  rmse: {better: lower, at_least: 0}\n",
}
HOUSE_PRICES = """\
name: House prices
workflow: unicon/kaggle@v1
inputs:
  predictions: {entrant: {label: Your predictions, max_size: 1MB}}
repeat:
  - inputs: [answers]
    tuples: [answers/public.csv, answers/private.csv]
scoring:
  groups:
    public_split: {answers: answers/public.csv}
    private_split: {answers: answers/private.csv}
  values:
    public: {block: max, over: {runs: public_split}, of: {run: [public_split, rmse]}}
    private: {block: max, over: {runs: private_split}, of: {run: [private_split, rmse]}}
  show:
    now: [public]
    after_close: [private]
"""
"""TASK-FORMAT.md 3.7's Kaggle task: one axis over the two splits, each
split's RMSE a value, the public one shown during the contest."""
HOUSE_DATA = {"answers/public.csv": b"1,100\n", "answers/private.csv": b"2,200\n"}
KAGGLE_BOARDS = """\
tasks:
  - {id: house-prices, marks: 2}
leaderboards:
  public_board:
    title: Public
    audience: {who: everyone}
    columns:
      pub: {block: lowest, from: {results: house-prices}, by: {value: [house-prices, public]}}
      public: {value: [{column: pub}, public]}
      public_at: {value: [{column: pub}, seconds]}
    sort: [{public: asc}, {public_at: asc}]
    display: [public, public_at]
  private_board:
    title: Private
    audience: {who: everyone}
    columns:
      pick:
        block: otherwise
        of:
          - block: lowest
            from: {results: house-prices}
            by: {value: [house-prices, private]}
            where: {value: [house-prices, marked]}
          - {block: lowest, from: {results: house-prices}, by: {value: [house-prices, public]}}
      private: {value: [{column: pick}, private]}
      at: {value: [{column: pick}, seconds]}
    sort: [{private: asc}, {at: asc}]
    display: [private, at]
"""
"""The leaderboards note's two Kaggle boards, lowest first: the public
one by each row's best public RMSE, the private one by the better of
its marked submissions' private RMSE, or, with none marked, the private
RMSE of its best public submission."""

GRADES = """\
tasks:
  - id: hw1
  - id: hw2
  - id: hw3
  - id: final
leaderboards:
  grades:
    audience: {who: entrants, rows: own}
    columns:
      hw1_score:
        block: otherwise
        of: [{value: [{block: latest, from: {results: hw1}}, score]}, 0]
      hw2_score:
        block: otherwise
        of: [{value: [{block: latest, from: {results: hw2}}, score]}, 0]
      hw3_score:
        block: otherwise
        of: [{value: [{block: latest, from: {results: hw3}}, score]}, 0]
      best:
        block: sum_best
        k: 2
        of: [{column: hw1_score}, {column: hw2_score}, {column: hw3_score}]
      final_score: {value: [{block: latest, from: {results: final}}, score]}
      total: {block: add, of: [{column: best}, {column: final_score}]}
    sort: [{total: desc}]
    display: [hw1_score, hw2_score, hw3_score, best, final_score, total]
"""
"""A course keeping the best two of three homework, and a final that
takes one submission, read as `latest(final).score`."""


def _judged(title: str, values: str, show: str, after: str = "") -> str:
    """A task over the islands workflow and its four tests, titled `title`,
    with `values` and `show` its scoring's, and `after` after them."""
    head = ISLANDS_TEXT[: ISLANDS_TEXT.index("scoring:")].replace("Islands", title)
    return f"{head}scoring:\n  values:\n{values}  show:\n{show}{after}"


SOLVED = _judged(
    "Task",
    "    solved: {block: min, over: {runs: all}, of: {run: [all, score]}}\n",
    "    now: [solved]\n",
)
"""ICPC's `a` and `b`: shows solved, the min of score, now."""
SCORED = "    score: {block: sum, over: {runs: all}, of: {run: [all, score]}}\n"
HOMEWORK = _judged("Homework", SCORED, "    now: [score]\n")
FINAL = _judged("Final", SCORED, "    now: [score]\n", "submissions: {max: 1}\n")
SEARCH = _judged(
    "Search", "    score: {block: entered, at_least: 0, at_most: 1}\n", "    now: [score, all]\n"
)
"""A search assignment marked by hand: its score a mark from 0 to 1 staff
enter, its tests shown with it."""
CARTPOLE_TEXT = TASKS["cartpole"][0]


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 26, hour, minute, tzinfo=UTC)


async def _workflows(acme: Acme) -> None:
    """The primitives, and the islands, cartpole and Kaggle workflows public
    at `v1`."""
    _primitives(acme)
    for name, held in (
        ("islands", TASKS["islands"][1]),
        ("cartpole", CARTPOLE_FILES),
        ("kaggle", KAGGLE_FILES),
    ):
        made = await acme.fake.workflows.create_workflow(
            PLATFORM, "unicon", name, dict(held), Visibility.PUBLIC
        )
        await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")


async def _task(
    setup: Setup,
    acme: Acme,
    name: str,
    text: str,
    data: Mapping[str, bytes] = ISLANDS_DATA,
    *,
    check: bool = True,
) -> TaskId:
    """The task `name` saved as `text` with `data` and published, each
    question the save asks answered; with `check`, its save row ended done,
    so its publication takes over."""
    task = await make_task(setup, acme, name)
    await _save(setup, acme, task, {"task.yaml": text.encode(), **data})
    if check:
        await checked(setup, task)
    return task


async def _entrant(setup: Setup, acme: Acme, user: int, name: str) -> Any:
    """`name`, signed in as `user`, alone in an approved entrant."""
    acme.fake.add_user(user, name)
    await register_entrant(setup, SPRING, user)
    return await signed_in(setup, acme.fake, user)


async def _submit(
    setup: Setup,
    acme: Acme,
    session: Any,
    task: TaskId,
    early: dict[str, Any],
    late: dict[str, Any] | None = None,
    *,
    field: str = "code",
) -> int:
    """A submission of `task` by `session`'s entrant through `field`, its
    early row ended done with `early` and, given `late`, its late row made
    and ended with it; its number."""
    made = await upload(setup, acme.fake, session, task, CODE, input=field)
    given = {field: SubmittedInput(uploads=(made.id,))}
    if field == "code":
        given["language"] = SubmittedInput(value="python")
    submitted = await submissions.submit(
        setup, session, task, given, idempotency_key=uuid.uuid4().hex
    )
    await setup.settle()
    [row] = [each for each in await _rows(setup, "early") if each.status != GradingStatus.DONE]
    await _ended(setup, row, early, late)
    return submitted.number


def _result(
    runs: Mapping[str, tuple[str, Mapping[str, Any]]], outcomes: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """A result version 6 holding `runs`, each with its verdict and
    reports, and the outcomes of the steps done once."""
    return {
        "schema_version": 6,
        "once": {"texts": {}, "reports": {}, "outcomes": dict(outcomes or {})},
        "runs": [
            {
                "id": run,
                "key": {},
                "texts": {},
                "passed": int(verdict == "accepted"),
                "reports": dict(reports),
                "verdict": verdict,
            }
            for run, (verdict, reports) in runs.items()
        ],
        "error": None,
        "staff": [],
        "run_log": None,
    }


def _tests(accepted: int, *, compiled: bool = True) -> dict[str, Any]:
    """A result of the four tests of a task over the islands workflow: the
    first `accepted` accepted, each scoring 1, the rest wrong answers; or,
    not `compiled`, a compile error, every test's verdict."""
    if not compiled:
        failed: tuple[str, Mapping[str, Any]] = ("compile_error", {})
        return _result({f"r{n}": failed for n in range(1, 5)}, {"compile": "compile_error"})
    runs = {
        f"r{n}": ("accepted", {"score": 1, "time_ms": 12, "memory_kb": 1500})
        if n <= accepted
        else ("wrong_answer", {})
        for n in range(1, 5)
    }
    return _result(runs, {"compile": "accepted"})


def _islands(verdicts: Mapping[str, str]) -> dict[str, Any]:
    """A result of islands' runs, each with its verdict, an accepted one
    scoring 1; the compile's outcome with the samples."""
    runs = {
        run: ("accepted", {"score": 1, "time_ms": 12, "memory_kb": 1500})
        if verdict == "accepted"
        else (verdict, {})
        for run, verdict in verdicts.items()
    }
    return _result(runs, {"compile": "accepted"} if "r1" in runs else {})


def _kaggle(rmse: float | None, run: str) -> dict[str, Any]:
    """A result of one split's run: its RMSE, or, for a malformed file, a
    wrong answer with none."""
    found = ("accepted", {"rmse": rmse}) if rmse is not None else ("wrong_answer", {})
    return _result({run: found})


def _cartpole(reward: int) -> dict[str, Any]:
    """A result of cartpole's six episodes, each with `reward`."""
    return _result({f"r{n}": ("accepted", {"reward": reward}) for n in range(1, 7)})


def _numbers(row: BoardRow) -> dict[str, Fraction | None]:
    return {cell.column: cell.number for cell in row.cells}


def _hidden(row: BoardRow) -> list[str]:
    return [cell.column for cell in row.cells if cell.hidden]


def _places(board: Board) -> list[tuple[str, int | None, int]]:
    return [(row.name, row.place, row.held) for row in board.rows]


async def _board(setup: Setup, session: Any, key: str) -> Board:
    [board] = await boards.seen(setup, session, SPRING, key)
    return board


async def test_ana_and_ben_rank_over_a_and_b_as_the_note_writes_through_the_hold(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    # The leaderboards note's 5.9: a contest from 10:00 to 15:00, its
    # hold from 14:00, 4:00 into it, until staff end it.
    clock.set(at(10, 5))
    await _workflows(acme)
    held = ICPC.replace("2026-06-01T13:00:00Z", "2026-09-26T14:00:00Z")
    await write_contest(acme.fake, BASE + held)
    a = await _task(setup, acme, "a", SOLVED)
    b = await _task(setup, acme, "b", SOLVED)
    ana = await _entrant(setup, acme, 20, "ana")
    ben = await _entrant(setup, acme, 21, "ben")

    # Ana: `a` wrong at 0:10, a compile error at 0:15, accepted at 0:21.
    for minute, result in ((10, _tests(3)), (15, _tests(0, compiled=False)), (21, _tests(4))):
        clock.set(at(10, minute))
        await _submit(setup, acme, ana, a, result)
    # Ben: `b` accepted at 4:30, after the hold's `from`.
    clock.set(at(14, 30))
    await _submit(setup, acme, ben, b, _tests(4))
    clock.set(at(14, 40))
    staff = await _staff(setup, acme)

    as_staff = await _board(setup, staff, "standings")
    as_ben = await _board(setup, ben, "standings")
    as_ana = await _board(setup, ana, "standings")

    # Staff: one solve each; Ana's a_tries is 1, the compile error no
    # try, and her a_time 21 + 20 = 41; Ben's 270; Ana first.
    assert _places(as_staff) == [("ana", 1, 0), ("ben", 2, 0)]
    first, second = as_staff.rows
    assert _numbers(first) == {
        "solved": 1,
        "penalty": 41,
        "last_solve": 21,
        "a_tries": 1,
        "a_time": 41,
        "b_tries": 0,
        "b_time": 0,
    }
    assert _numbers(second) == {
        "solved": 1,
        "penalty": 270,
        "last_solve": 270,
        "a_tries": 0,
        "a_time": 0,
        "b_tries": 0,
        "b_time": 270,
    }
    # Ben reads both rows live, one solve each, Ana first. The task shows
    # solved and no runs, so `compiled` is not shown, and the tries, the
    # times and the penalty that read it are hidden from anyone but
    # staff (stage 5 section 6, items 82 and 93, left as the spec writes
    # them): the penalty is no sort key for him, so no places.
    assert _places(as_ben) == [("ana", None, 0), ("ben", None, 0)]
    assert [_hidden(row) for row in as_ben.rows] == [
        ["penalty", "a_tries", "a_time"],
        ["penalty", "b_tries", "b_time"],
    ]
    assert [_numbers(row)["solved"] for row in as_ben.rows] == [1, 1]
    assert [_numbers(row)["last_solve"] for row in as_ben.rows] == [21, 270]
    assert as_ben.dropped == ("penalty",)
    # Ana sees Ben's `b` results empty: 0 solved and one held, and herself
    # first.
    assert _places(as_ana) == [("ana", None, 0), ("ben", None, 1)]
    assert _numbers(as_ana.rows[1])["solved"] == 0
    assert as_ana.held_from == at(14, 0)

    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)
    assert await boards.end_hold(setup, manager, SPRING, confirmed=True)

    # The hold ended: every viewer sees what Ben saw.
    for session in (ana, ben, None):
        board = await _board(setup, session, "standings")
        assert _places(board) == _places(as_ben)
        assert [_numbers(row) for row in board.rows] == [_numbers(row) for row in as_ben.rows]
        assert [_hidden(row) for row in board.rows] == [_hidden(row) for row in as_ben.rows]


async def test_islands_with_its_checks_ranks_on_the_olympiad_board_by_the_best_of_each(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(10, 5))
    await _workflows(acme)
    await write_contest(acme.fake, BASE + OLYMPIAD)
    islands = await _task(
        setup, acme, "islands", ISLANDS_TEXT + CHECKS, {**ISLANDS_DATA, **CHECK_FILES}, check=False
    )
    [save] = await _rows(setup, "save")
    await _checks_run(setup, acme, save.id)
    assert await _report(setup, save.id, "save") is GradingStatus.DONE
    await setup.settle()
    ana = await _entrant(setup, acme, 20, "ana")
    ben = await _entrant(setup, acme, 21, "ben")

    samples = _islands({"r1": "accepted", "r2": "accepted"})
    clock.set(at(11, 0))
    await _submit(setup, acme, ana, islands, samples, _islands({"r3": "accepted", "r4": "wrong"}))
    await _submit(setup, acme, ben, islands, samples, _islands({"r3": "accepted", "r4": "wrong"}))
    clock.set(at(12, 0))
    every = _islands({"r3": "accepted", "r4": "accepted"})
    await _submit(setup, acme, ana, islands, samples, every)
    staff = await _staff(setup, acme)

    # Before the reveal the total, st1 and st2 are shown to no entrant, so
    # the column reading them is hidden on every row, not points so far, and
    # no sort key: no places.
    clock.set(at(14, 50))
    before = await _board(setup, ana, "results")
    assert _places(before) == [("ana", None, 0), ("ben", None, 0)]
    assert [_numbers(row) for row in before.rows] == [{"islands_score": None}] * 2
    assert [_hidden(row) for row in before.rows] == [["islands_score"]] * 2
    # Staff read every value at once.
    assert [_numbers(row) for row in (await _board(setup, staff, "results")).rows] == [
        {"islands_score": 100},
        {"islands_score": 40},
    ]

    clock.set(at(15, 5))
    after = await _board(setup, None, "results")
    assert _places(after) == [("ana", 1, 0), ("ben", 2, 0)]
    assert [_numbers(row) for row in after.rows] == [{"islands_score": 100}, {"islands_score": 40}]


async def test_the_course_gradebook_reads_cartpole_and_the_search_mark_entered_from_the_queue(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(10, 5))
    await _workflows(acme)
    course = GRADEBOOK.replace("  - id: search\n", "  - {id: search, marks: 1}\n")
    await write_contest(acme.fake, BASE + course)
    search = await _task(setup, acme, "search", SEARCH)
    cartpole = await _task(setup, acme, "cartpole", CARTPOLE_TEXT, {})
    ana = await _entrant(setup, acme, 20, "ana")
    ben = await _entrant(setup, acme, 21, "ben")

    for minute in (0, 1):
        clock.set(at(11, minute))
        await _submit(setup, acme, ana, search, _tests(4))
    await marks.mark(setup, ana, search, 1)
    for minute, session, reward in ((2, ana, 300), (3, ana, 500), (4, ben, 237)):
        clock.set(at(11, minute))
        await _submit(setup, acme, session, cartpole, _cartpole(reward), field="weights")

    # Everyone reads cartpole_ranking, each row's best reward shown now.
    ranking = await _board(setup, None, "cartpole_ranking")
    assert _places(ranking) == [("ana", 1, 0), ("ben", 2, 0)]
    assert [_numbers(row) for row in ranking.rows] == [{"reward": 500}, {"reward": 237}]
    # Ana reads her own gradebook row, with no place (item 86): no mark
    # yet, so search_mark is 0, and cartpole counts in full, 500 being
    # past 475.
    [mine] = (await _board(setup, ana, "gradebook")).rows
    assert (mine.name, mine.place) == ("ana", None)
    assert _numbers(mine) == {"search_mark": 0, "cartpole_best": 500, "total": Fraction(1, 2)}

    # After the close the queue lists her marked submission, counted by
    # the gradebook's search_mark and the total reading it, and a manager enters its mark there.
    clock.set(at(15, 5))
    manager = await organiser(setup, acme.fake, 7, CONTEST, Role.MANAGER)
    [row] = await entries.queue(setup, manager, search)
    assert (row.name, row.submissions) == (
        "ana",
        (
            entries.Queued(
                1, (("gradebook", "search_mark"), ("gradebook", "total")), (("score", None),)
            ),
        ),
    )
    await entries.enter(setup, manager, search, row.owner, 1, "score", "0.8")

    [mine] = (await _board(setup, ana, "gradebook")).rows
    assert _numbers(mine) == {
        "search_mark": Fraction(4, 5),
        "cartpole_best": 500,
        "total": Fraction(9, 10),
    }
    [bens] = (await _board(setup, ben, "gradebook")).rows
    assert _numbers(bens) == {
        "search_mark": 0,
        "cartpole_best": 237,
        "total": Fraction(237, 950),
    }


async def test_kaggle_ranks_each_split_lowest_first_and_a_malformed_file_nowhere(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(10, 5))
    await _workflows(acme)
    await write_contest(acme.fake, BASE + KAGGLE_BOARDS)
    house = await _task(setup, acme, "house-prices", HOUSE_PRICES, HOUSE_DATA)
    ana = await _entrant(setup, acme, 20, "ana")
    ben = await _entrant(setup, acme, 21, "ben")
    cy = await _entrant(setup, acme, 22, "cy")

    async def predicted(session: Any, public: float | None, private: float | None) -> int:
        early, late = _kaggle(public, "r1"), _kaggle(private, "r2")
        return await _submit(setup, acme, session, house, early, late, field="predictions")

    clock.set(at(11, 0))
    await predicted(ana, 0.3, 0.4)
    await predicted(ben, 0.25, 0.35)
    clock.set(at(12, 0))
    await predicted(ana, 0.2, 0.5)
    await predicted(cy, None, None)
    clock.set(at(12, 1))
    malformed = await predicted(ana, None, None)
    # Ana marks her first and the malformed one; Ben marks none.
    await marks.mark(setup, ana, house, 1)
    await marks.mark(setup, ana, house, malformed)

    # The public board ranks by each row's best public RMSE, lowest first:
    # Ana's second, at 12:00, then Ben's. Cy's only submission has no
    # RMSE, so no pick, and his row sorts last with nothing in it.
    public = await _board(setup, None, "public_board")
    assert [row.name for row in public.rows] == ["ana", "ben", "cy"]
    assert [_numbers(row) for row in public.rows] == [
        {"public": Fraction(1, 5), "public_at": 7200},
        {"public": Fraction(1, 4), "public_at": 3600},
        {"public": None, "public_at": None},
    ]
    assert [row.place for row in public.rows[:2]] == [1, 2]
    # The private board picks by the private RMSE, shown only after the
    # close, so before it Ana's and Ben's cells are hidden and nothing is
    # placed. Cy's pick compared no private RMSE, having no mark and no
    # public one, so his row hides nothing.
    private = await _board(setup, None, "private_board")
    assert [_hidden(row) for row in private.rows] == [["private", "at"]] * 2 + [[]]
    assert {row.place for row in private.rows} == {None}

    # After the close: Ben's best public submission, being marked by
    # nobody, gives his private 0.35; Ana's marks give 0.4, her malformed
    # one skipped, not counted as best; Cy still last.
    clock.set(at(15, 5))
    private = await _board(setup, None, "private_board")
    assert [row.name for row in private.rows] == ["ben", "ana", "cy"]
    assert [_numbers(row)["private"] for row in private.rows] == [
        Fraction(7, 20),
        Fraction(2, 5),
        None,
    ]
    assert [row.place for row in private.rows[:2]] == [1, 2]


async def test_a_course_keeps_the_best_two_homework_and_a_final_takes_one_submission(
    setup: Setup, acme: Acme, spring: str, clock: FakeClock
) -> None:
    clock.set(at(10, 5))
    await _workflows(acme)
    await write_contest(acme.fake, BASE + GRADES)
    homework = [await _task(setup, acme, f"hw{n}", HOMEWORK) for n in (1, 2, 3)]
    final = await _task(setup, acme, "final", FINAL)
    ana = await _entrant(setup, acme, 20, "ana")

    clock.set(at(11, 0))
    for task, accepted in zip(homework, (4, 1, 3), strict=True):
        await _submit(setup, acme, ana, task, _tests(accepted))
    [before] = (await _board(setup, ana, "grades")).rows
    await _submit(setup, acme, ana, final, _tests(2))
    with pytest.raises(SubmissionLimit):
        await _submit(setup, acme, ana, final, _tests(4))

    # The best two of 4, 1 and 3 are 7; with no final, the total is
    # missing, not points so far.
    assert _numbers(before) == {
        "hw1_score": 4,
        "hw2_score": 1,
        "hw3_score": 3,
        "best": 7,
        "final_score": None,
        "total": None,
    }
    [after] = (await _board(setup, ana, "grades")).rows
    assert (_numbers(after)["final_score"], _numbers(after)["total"]) == (Fraction(2), Fraction(9))
