"""A row's marks (C1): up to the task's `marks` of its own submissions,
frozen at its close, and only while a leaderboard reads which of the
task's submissions an entrant marks; a task's `marks` never lowered below
what a row holds. And an organiser's read of an entrant's submission,
which shows what the entrant's own read holds back.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import update

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
from forge.domain.names import EntrantOwner
from forge.domain.roles import Role, RoleGrant, Scope
from forge.domain.sessions import Session
from forge.domain.submission_state import SubmissionState
from forge.domain.submissions import SubmittedInput
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import (
    contest_home,
    entrants,
    files,
    gradings,
    identity,
    marks,
    submissions,
    task_save,
)
from forge.services.access import Organiser
from forge.services.task_save import Draft, Published
from forge.testing import FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    checked,
    organiser,
    signed_in,
    upload,
    write_contest,
)

END = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

LEADERBOARDS = """\
leaderboards:
  final:
    title: Final
    columns:
      picked: {block: latest, from: {results: sum}, where: {value: [sum, marked]}}
      points: {block: otherwise, of: [{value: [{column: picked}, score]}, 0]}
    sort: [{points: desc}]
    display: [points]
"""
"""A leaderboard reading which of sum's submissions an entrant marks."""

PASSED = b"    passed: {block: sum, over: {runs: all}, of: {run: [all, passed]}}\n"
"""A value of sum's counting its runs passed, shown once the task closes."""


def _result(verdict: str) -> dict[str, Any]:
    return {
        "schema_version": 6,
        "runs": [
            {
                "id": "r1",
                "key": {"input": "tests/1.in", "answer": "tests/1.ans"},
                "verdict": verdict,
                "passed": 1 if verdict == "accepted" else 0,
                "reports": {},
                "texts": {},
            }
        ],
    }


RESULT = _result("wrong_answer")
EVERY_RUN = _result("accepted")


def contest(extra: str = "", entry: str = "") -> str:
    """The running contest, with `entry` on sum's entry and `extra` after."""
    return RUNNING.format(visibility="everyone") + entry + extra


async def _task_yaml(acme: Acme, task: TaskId) -> bytes:
    return (await acme.fake.content.read_file(PLATFORM, task, "task.yaml")).content


async def _save(
    setup: Setup, acme: Acme, task: TaskId, files_: Mapping[str, bytes]
) -> Published | Draft:
    head = await acme.fake.content.list_files(PLATFORM, task)
    return await task_save.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens.get(path)) for path, content in files_.items()},
        confirm=True,
    )


async def _passed_after_close(setup: Setup, acme: Acme, task: TaskId) -> Published:
    """sum with a value `passed` it shows once it closes."""
    current = (
        (await _task_yaml(acme, task))
        .replace(b"  values:\n", b"  values:\n" + PASSED)
        .replace(
            b"    now: [score, all, public/]\n",
            b"    now: [score, all, public/]\n    after_close: [passed]\n",
        )
    )
    saved = await _save(setup, acme, task, {"task.yaml": current})
    assert isinstance(saved, Published), saved
    await checked(setup, task)
    return saved


async def _submit(setup: Setup, acme: Acme, entered: Entered, key: str) -> int:
    return await _submit_as(setup, acme, entered.session, entered.task, key)


async def _submit_as(setup: Setup, acme: Acme, session: Session, task: TaskId, key: str) -> int:
    made = await upload(setup, acme.fake, session, task, key.encode())
    submission = await submissions.submit(
        setup,
        session,
        task,
        {
            "code": SubmittedInput(uploads=(made.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key=key,
    )
    return submission.number


async def _graded(setup: Setup, number: int, result: dict[str, Any]) -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.submission_number == number, Grading.pipeline != "save")
            .values(status="done", result=result)
        )


@pytest.fixture
async def ranked(setup: Setup, acme: Acme, entered: Entered, clock: FakeClock) -> Entered:
    """bob's two graded submissions to sum, under the leaderboard: the first,
    at 12:00, fails its run; the second, at 12:01, passes it.
    """
    await write_contest(acme.fake, contest(LEADERBOARDS))
    await _passed_after_close(setup, acme, entered.task)
    first = await _submit(setup, acme, entered, "key-0001-aaaa")
    await _graded(setup, first, RESULT)
    clock.advance(timedelta(minutes=1))
    second = await _submit(setup, acme, entered, "key-0002-bbbb")
    await _graded(setup, second, EVERY_RUN)
    return entered


async def test_a_contest_stored_with_the_retired_on_system_error_still_reads_everywhere(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    # A form of an earlier release wrote it; nothing took its place.
    await write_contest(acme.fake, contest(LEADERBOARDS) + "on_system_error: grading\n")

    (task,) = (await contest_home.home(setup, ranked.session, SPRING)).tasks

    assert task.name == "sum"
    assert (await contest_home.task(setup, ranked.session, ranked.task)).marks == 1
    assert (await marks.held(setup, ranked.session, ranked.task)).most == 1


async def _contestant(setup: Setup, acme: Acme, user_id: int, name: str) -> Session:
    acme.fake.add_user(user_id, name)
    session = await signed_in(setup, acme.fake, user_id)
    entrant = await entrants.register(setup, session, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await entrants.approve(setup, manager, SPRING, entrant.id)
    return session


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

    unmarked = await marks.unmark(setup, ranked.session, ranked.task, 2)
    assert unmarked.numbers == ()
    await marks.mark(setup, ranked.session, ranked.task, 1)
    clock.set(END)
    await identity.current(ranked.session.id, setup=setup)
    with pytest.raises(MarksFrozen):
        await marks.unmark(setup, ranked.session, ranked.task, 1)
    assert (await marks.held(setup, ranked.session, ranked.task)).frozen


async def test_a_task_no_leaderboard_reads_the_marks_of_takes_no_marks(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await write_contest(acme.fake, contest())
    with pytest.raises(MarksOff):
        await marks.held(setup, entered.session, entered.task)
    assert (await contest_home.task(setup, entered.session, entered.task)).marks is None

    await write_contest(acme.fake, contest(LEADERBOARDS))
    assert (await contest_home.task(setup, entered.session, entered.task)).marks == 1


async def test_someone_not_approved_is_not_told_whether_a_task_takes_marks(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    for settings in (contest(), contest(LEADERBOARDS)):
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
        setup, acme.ada, SPRING, "contest.yaml", content.encode(), current.token, confirm=True
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
    await write_contest(acme.fake, contest(LEADERBOARDS, "    marks: 2\n"))
    await marks.mark(setup, ranked.session, ranked.task, 1)
    await marks.mark(setup, ranked.session, ranked.task, 2)

    refused = await _refused(setup, acme, contest(LEADERBOARDS, "    marks: 1\n"))
    dropped = await _refused(setup, acme, contest(LEADERBOARDS))

    said = "1 entrant has marked 2 submissions on sum, so it cannot allow fewer marks."
    assert refused == [("tasks[0].marks", said)]
    assert dropped == [("tasks[0]", said)]
    assert await _write(setup, acme, contest(LEADERBOARDS, "    marks: 3\n"))


def _observer_of(acme: Acme, scope: Scope) -> Organiser:
    """Someone checked as an observer at `scope` and nothing else."""
    return Organiser(
        user=User(id=9, username="eve"),
        grants=(RoleGrant(scope, Role.OBSERVER),),
        scope=scope,
        role=Role.OBSERVER,
        identity=acme.ada.identity,
    )


async def test_an_organiser_reads_a_value_its_entrant_is_shown_only_after_the_close(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    observer = _observer_of(acme, Scope("acme", "spring", "sum"))

    seen = await submissions.organised(
        setup, observer, ranked.task, EntrantOwner(ranked.entrant), 1
    )
    own = await submissions.one(setup, ranked.session, ranked.task, 1)

    assert seen.grading is not None and own.grading is not None
    assert seen.grading.status is GradingStatus.DONE
    assert own.grading.status is SubmissionState.GRADED
    seen_values = {value.name: value for value in seen.grading.run_values}
    assert (seen_values["passed"].number, seen_values["passed"].show) == (
        Fraction(0),
        "after_close",
    )
    assert [value.name for value in own.grading.run_values] == ["score"]
    assert own.grading.reveal_at == END
    assert (seen.number, seen.submitted_at) == (own.number, own.submitted_at)


async def test_a_contestants_own_read_is_unchanged_by_an_organisers(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    before = await submissions.one(setup, ranked.session, ranked.task, 1)

    await submissions.organised(
        setup,
        _observer_of(acme, Scope("acme", "spring")),
        ranked.task,
        EntrantOwner(ranked.entrant),
        1,
    )
    after = await submissions.one(setup, ranked.session, ranked.task, 1)

    assert after == before
    assert before.grading is not None
    assert [value.name for value in before.grading.run_values] == ["score"]
    assert before.grading.reveal_at == END


async def test_an_organiser_without_the_observer_role_at_the_task_is_refused(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    elsewhere = _observer_of(acme, Scope("acme", "spring", "product"))

    with pytest.raises(Forbidden):
        await submissions.organised(setup, elsewhere, ranked.task, EntrantOwner(ranked.entrant), 1)
    with pytest.raises(Forbidden):
        await gradings.list_gradings(setup, elsewhere, ranked.task)


async def test_a_row_with_no_such_submission_is_no_such_submission(
    setup: Setup, acme: Acme, ranked: Entered
) -> None:
    observer = _observer_of(acme, Scope("acme", "spring"))

    for owner, number in ((EntrantOwner(ranked.entrant), 3), (EntrantOwner(uuid.uuid4()), 1)):
        with pytest.raises(NotFound):
            await submissions.organised(setup, observer, ranked.task, owner, number)


async def test_a_mark_in_flight_and_a_save_lowering_marks_never_leave_a_row_over_them(
    setup: Setup, acme: Acme, ranked: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mark holds the contest's rules before it reads how many marks the
    task takes, so a save lowering them waits and counts the new mark, or
    the mark reads the lowered number (C1)."""
    await write_contest(acme.fake, contest(LEADERBOARDS, "    marks: 3\n"))
    await marks.mark(setup, ranked.session, ranked.task, 1)
    change = marks._change
    saving: list[asyncio.Task[Any]] = []

    async def save_meanwhile(*args: Any, **kwargs: Any) -> Any:
        lowered = contest(LEADERBOARDS, "    marks: 1\n")
        saving.append(asyncio.create_task(_write(setup, acme, lowered)))
        await asyncio.wait(saving, timeout=3)
        return await change(*args, **kwargs)

    monkeypatch.setattr(marks, "_change", save_meanwhile)
    with contextlib.suppress(MarkLimit):
        await marks.mark(setup, ranked.session, ranked.task, 2)
    saved = await asyncio.gather(*saving, return_exceptions=True)

    held = len((await marks.held(setup, ranked.session, ranked.task)).numbers)
    allowed = 1 if not isinstance(saved[0], BaseException) else 3
    assert held <= allowed, (held, saved)
