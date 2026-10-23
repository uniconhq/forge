"""The checks across files (stage 5, phase 5.7; the leaderboards note's
6.2): a save of the contest's settings refuses a board reading a value a
task's live publication does not give, choosing which submission counts by
a value reading a mark, or giving `best_each` a value it cannot work out
again, each at its path, as it refuses a loop across boards and a pick
column sorted by; a task with no live publication yet is checked at its
first save instead; a task's save refuses dropping a value a board reads,
or making one a board chooses by read a mark, naming the board and the
column; a save of the contest made while a task's newest save is still
being checked is checked against that one too, since it takes over with no
save to check the boards again; and the hold's `from` stays inside the
contest, a save moving the end before it refused, and is never set to a
time passed.
"""

from collections.abc import Mapping

import pytest
from sqlalchemy import update

from forge.db.tables import Grading
from forge.domain.content import Edit
from forge.domain.errors import ConfirmationRequired
from forge.domain.grading import GradingStatus
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import boards, files, publications
from forge.services.publications import Draft, Published
from tests.services.conftest import RUNNING, SPRING, Acme, make_task, write_contest
from tests.services.test_run_task_saves import TASKS, _primitives, checked

DRAFT = (
    RUNNING.format(visibility="everyone")
    .replace("state: published", "state: draft")
    .replace("  - id: sum\n", "  - id: islands\n")
)
"""acme/spring, not started, listing islands."""

MARKED = TASKS["islands"][0].replace(
    "  show:",
    "    review: {block: entered, at_least: 0, at_most: 10}\n"
    "    graded: {block: add, of: [{value: total}, {value: review}]}\n"
    "  show:",
)
"""islands with a mark staff enter and a value reading it."""

BOARD = """\
leaderboards:
  results:
    title: Results
    columns:
      best:
        block: best_each
        from: {results: islands}
        value: {name: total}
        directions: {st1: desc, st2: desc}
      top: {block: highest, from: {results: islands}, by: {value: [islands, st1]}}
      graded: {block: otherwise, of: [{value: [{column: top}, graded]}, 0]}
    sort: [{best: desc}]
    display: [best, graded]
"""
"""A board working islands' total out again from its best st1 and st2,
choosing a submission by st1 and reading its mark through it."""


async def _made(acme: Acme, setup: Setup, contest: str = DRAFT) -> TaskId:
    """islands made, not yet saved, its workflow public at `v1`, in
    `contest`."""
    task = await make_task(setup, acme, "islands")
    _primitives(acme)
    _, held, _ = TASKS["islands"]
    workflow = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", "islands", dict(held), Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    await write_contest(acme.fake, contest)
    return task


async def _save(
    setup: Setup, acme: Acme, task: TaskId, text: str, data: Mapping[str, bytes] | None = None
) -> Published | Draft:
    """islands saved as `text`, each question the save asks answered."""
    head = await acme.fake.content.list_files(PLATFORM, task)
    contents = {"task.yaml": text.encode(), **(data or {})}
    edits = {path: Edit(content, head.tokens.get(path)) for path, content in contents.items()}
    try:
        return await publications.save(setup, acme.ada, task, edits, confirm=True)
    except ConfirmationRequired as asked:
        return await publications.save(
            setup, acme.ada, task, edits, confirm=True, asked=asked.extra["asked"]
        )


async def _live(setup: Setup, acme: Acme, text: str = MARKED) -> TaskId:
    """islands published as `text` and taken over, in the draft contest."""
    task = await _made(acme, setup)
    saved = await _save(setup, acme, task, text, TASKS["islands"][2])
    assert isinstance(saved, Published), saved
    await checked(setup, task)
    return task


async def _write(setup: Setup, acme: Acme, content: str) -> None:
    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    await files.write(setup, acme.ada, SPRING, "contest.yaml", content.encode(), current.token)


async def _refused(setup: Setup, acme: Acme, content: str) -> list[tuple[str, str]]:
    with pytest.raises(InvalidDefinition) as refused:
        await _write(setup, acme, content)
    return [(error["path"], error["message"]) for error in refused.value.errors]


def _errors(result: Published | Draft) -> list[tuple[str, str]]:
    assert isinstance(result, Draft), result
    return [(error["path"], error["message"]) for error in result.errors]


def _column(tree: str, sort: str = "") -> str:
    return f"leaderboards:\n  s:\n    columns:\n      c: {tree}\n{sort}"


@pytest.mark.parametrize(
    ("board", "path"),
    [
        # A value islands does not give.
        (
            _column("{value: [{block: latest, from: {results: islands}}, nope]}"),
            "leaderboards.s.columns.c.value[1]",
        ),
        # A pick by a mark, and max of one.
        (
            _column("{block: highest, from: {results: islands}, by: {value: [islands, graded]}}"),
            "leaderboards.s.columns.c.by",
        ),
        (
            _column("{block: max, over: {results: islands}, of: {value: [islands, review]}}"),
            "leaderboards.s.columns.c.of",
        ),
        # best_each over a value folding runs, and with a direction left out.
        (
            _column(
                "{block: best_each, from: {results: islands}, value: {name: sample_time}, "
                "directions: {st1: desc}}"
            ),
            "leaderboards.s.columns.c.value",
        ),
        (
            _column(
                "{block: best_each, from: {results: islands}, value: {name: total}, "
                "directions: {st1: desc}}"
            ),
            "leaderboards.s.columns.c.directions",
        ),
        # A loop across boards, and a pick column sorted by.
        (
            "leaderboards:\n  s: {columns: {c: {column: [t, d]}}}\n"
            "  t: {columns: {d: {column: [s, c]}}}\n",
            "leaderboards.s.columns.c",
        ),
        (
            _column("{block: first, from: {results: islands}}", "    sort: [{c: desc}]\n"),
            "leaderboards.s.sort[0]",
        ),
    ],
)
async def test_each_refusal_at_the_contests_save(
    setup: Setup, acme: Acme, spring: str, board: str, path: str
) -> None:
    await _live(setup, acme)

    found = await _refused(setup, acme, DRAFT + board)

    assert path in [each for each, _ in found]


async def test_a_board_reading_what_islands_gives_is_saved_and_names_resolve_against_the_live_one(
    setup: Setup, acme: Acme, spring: str
) -> None:
    await _live(setup, acme)
    await _write(setup, acme, DRAFT + BOARD)

    found = await _refused(
        setup, acme, DRAFT + BOARD.replace("{value: [islands, st1]}", "{value: [islands, st3]}")
    )

    assert found == [
        (
            "leaderboards.results.columns.top.by.value[1]",
            "islands has no value called st3. Its values are graded, review, sample_score, "
            "sample_time, st1, st2 and total. A leaderboard also reads the facts attempt, "
            "seconds, seconds_late, marked and compiled.",
        )
    ]


async def test_a_task_not_yet_published_is_checked_at_its_first_save(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await _made(acme, setup)
    reading = DRAFT + _column("{value: [{block: latest, from: {results: islands}}, st3]}")
    await _write(setup, acme, reading)

    first = await _save(setup, acme, task, MARKED, TASKS["islands"][2])

    assert _errors(first) == [
        (
            "scoring.values",
            "The leaderboard s reads st3 of islands in its column c, so islands must keep a "
            "value called st3. Take it out of that column first.",
        )
    ]


@pytest.mark.parametrize(
    ("old", "new", "path", "words"),
    [
        # total dropped, or renamed.
        (
            "    total:\n",
            "    overall:\n",
            "scoring.values",
            "The leaderboard Results reads total of islands in its column best, so islands must "
            "keep a value called total. Take it out of that column first.",
        ),
        # st1, which top chooses by, now reading the mark.
        (
            "    st1: {block: min, over: {runs: st1_tests}, of: {run: [st1_tests, score]}}",
            "    st1: {block: mul, of: [{value: review}, 1]}",
            "scoring.values.st1",
            "The leaderboard Results chooses which submission of islands counts by st1 in its "
            "column top, so st1 cannot read a mark staff enter: the grading queue would wait on "
            "the mark itself. Change that column first.",
        ),
        # total, which best works out again, reading a fact, or a value
        # given no direction.
        (
            "        - {block: mul, of: [60, {value: st2}]}",
            "        - {block: mul, of: [60, {value: st2}]}\n"
            "        - {block: mul, of: [-1, {fact: seconds_late}]}",
            "scoring.values.total",
            "The leaderboard Results works total of islands out again with best_each in its "
            "column best, so total must read only other values and numbers directly, not the "
            "fact seconds_late. Change that column first.",
        ),
        (
            "        - {block: mul, of: [60, {value: st2}]}",
            "        - {block: mul, of: [60, {value: st2}]}\n        - {value: sample_score}",
            "scoring.values.total",
            "The leaderboard Results works total of islands out again with best_each in its "
            "column best, which gives no direction for sample_score. Give it one there first.",
        ),
    ],
)
async def test_each_refusal_at_a_tasks_save(
    setup: Setup, acme: Acme, spring: str, old: str, new: str, path: str, words: str
) -> None:
    task = await _live(setup, acme)
    await _write(setup, acme, DRAFT + BOARD)
    assert old in MARKED
    changed = MARKED.replace(old, new)
    if old == "    total:\n":
        changed = changed.replace("st2, total, all]", "st2, overall, all]").replace(
            "{value: total}", "{value: overall}"
        )

    saved = await _save(setup, acme, task, changed)

    # A value best works total out again from is refused there too.
    assert (path, words) in _errors(saved)
    assert {each for each, _ in _errors(saved)} == {path}
    assert isinstance(await _save(setup, acme, task, MARKED), Published)


async def test_a_tasks_save_says_each_refusal_once_when_a_column_reads_the_value_twice(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await _live(setup, acme)
    twice = _column(
        "{block: add, of: [{value: [{block: latest, from: {results: islands}}, review]}, "
        "{value: [{block: first, from: {results: islands}}, review]}]}"
    )
    await _write(setup, acme, DRAFT + twice)

    saved = await _save(setup, acme, task, TASKS["islands"][0])

    assert _errors(saved) == [
        (
            "scoring.values",
            "The leaderboard s reads review of islands in its column c, so islands must keep a "
            "value called review. Take it out of that column first.",
        )
    ]


async def test_the_holds_from_stays_inside_the_contest_and_is_never_set_to_a_time_passed(
    setup: Setup, acme: Acme, spring: str
) -> None:
    await _live(setup, acme)
    held = DRAFT + "hold: {from: 2026-09-26T14:00:00Z, until: staff}\n"
    passed = held.replace("T14:00", "T11:00")
    late = held.replace("T14:00", "T15:00")
    sooner = held.replace("end: 2026-09-26T15:00:00Z", "end: 2026-09-26T13:30:00Z")

    assert [path for path, _ in await _refused(setup, acme, passed)] == ["hold.from"]
    assert [path for path, _ in await _refused(setup, acme, late)] == ["hold.from"]
    await _write(setup, acme, held)
    assert await _refused(setup, acme, sooner) == [
        (
            "end",
            "The leaderboards' hold starts on 26 September 2026 at 14:00 UTC, so the contest "
            "must end after it. Move the hold's from first, or take the hold out.",
        )
    ]
    await _write(
        setup, acme, held.replace("end: 2026-09-26T15:00:00Z", "end: 2026-09-26T16:00:00Z")
    )


LATEST_GRADED = _column(
    "{block: otherwise, of: [{value: [{block: latest, from: {results: islands}}, graded]}, 0]}"
)
"""A board reading islands' graded on its latest submission."""


async def _check_refused(setup: Setup, publication: str) -> None:
    """The save row checking `publication` ended `done` with a run not
    accepted, so it never takes over."""
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.publication_id == publication, Grading.pipeline == "save")
            .values(
                status=GradingStatus.DONE,
                result={"schema_version": 6, "runs": [{"verdict": "wrong_answer"}]},
            )
        )


async def test_a_board_reading_a_value_a_save_still_checked_drops_is_refused_until_it_is_refused(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await _live(setup, acme)
    dropped = await _save(setup, acme, task, TASKS["islands"][0])
    assert isinstance(dropped, Published) and dropped.check == "checking"

    found = await _refused(setup, acme, DRAFT + LATEST_GRADED)

    assert found == [
        (
            "leaderboards.s.columns.c.of[0].value[1]",
            "islands has no value called graded. Its values are sample_score, sample_time, st1, "
            "st2 and total. A leaderboard also reads the facts attempt, seconds, seconds_late, "
            "marked and compiled. " + boards.CHECKING.format(task="islands"),
        )
    ]
    # A value neither gives is refused once, as the live one says it.
    assert await _refused(
        setup, acme, DRAFT + _column("{value: [{block: latest, from: {results: islands}}, nope]}")
    ) == [
        (
            "leaderboards.s.columns.c.value[1]",
            "islands has no value called nope. Its values are graded, review, sample_score, "
            "sample_time, st1, st2 and total. A leaderboard also reads the facts attempt, "
            "seconds, seconds_late, marked and compiled.",
        )
    ]
    # Once its check is refused it never takes over, and the live one gives
    # graded.
    await _check_refused(setup, dropped.publication)
    await _write(setup, acme, DRAFT + LATEST_GRADED)


async def test_a_board_choosing_by_a_value_a_save_still_checked_makes_read_a_mark_is_refused(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await _live(setup, acme)
    marking = MARKED.replace(
        "    st1: {block: min, over: {runs: st1_tests}, of: {run: [st1_tests, score]}}",
        "    st1: {block: mul, of: [{value: review}, 1]}",
    )
    assert marking != MARKED
    assert isinstance(await _save(setup, acme, task, marking), Published)

    found = await _refused(
        setup,
        acme,
        DRAFT + _column("{block: highest, from: {results: islands}, by: {value: [islands, st1]}}"),
    )

    assert [path for path, _ in found] == ["leaderboards.s.columns.c.by"]
    assert found[0][1].startswith("highest chooses which submission of islands counts by st1")
    assert found[0][1].endswith(boards.CHECKING.format(task="islands"))


async def test_a_board_reading_a_value_a_first_save_still_checked_does_not_give_is_refused(
    setup: Setup, acme: Acme, spring: str
) -> None:
    task = await _made(acme, setup)
    first = await _save(setup, acme, task, MARKED, TASKS["islands"][2])
    assert isinstance(first, Published) and first.check == "checking"

    found = await _refused(
        setup, acme, DRAFT + _column("{value: [{block: latest, from: {results: islands}}, nope]}")
    )

    assert [path for path, _ in found] == ["leaderboards.s.columns.c.value[1]"]
    assert "islands has no value called nope." in found[0][1]
