"""A task's save (SIMPLER-CORE.md 8, 9 and 11): the islands, cartpole
and tour tasks publish their runs file and their plan, version 6, pinned to
the one commit the workflow's version stood at, every workflow file read
there; a save again says what its plan changed and grades nothing again; a
runs file that does not read refuses the save, and so does an empty
`scoring`; a self-reported report asks once, and the save is refused
until that question is answered; what was shown is not hidden again; the
files `show` lists are served from their time on, and no other, the
reveal's from its instant and a release's not yet; the contest gives a
task a due and a leaderboard, and a stored worth or late share is ignored;
and an entrant submits to one in parts, each part's files downloaded and,
when the submit's answer was lost, its uploads used up.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from forge.db.tables import Grading, Upload
from forge.domain.content import Edit
from forge.domain.errors import (
    ConfirmationRequired,
    InvalidInputs,
    NotFound,
    TooLarge,
    Unavailable,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import TaskId
from forge.domain.names import EntrantOwner
from forge.domain.plan_basics import PLAN_PATH
from forge.domain.roles import Role, Scope
from forge.domain.run_plan import FileFact, RunPlan
from forge.domain.run_task import RUNS_PATH, RunsFile
from forge.domain.submissions import SubmittedInput
from forge.domain.workflows import Visibility
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.setup import Setup
from forge.services import (
    contest_home,
    files,
    gradings,
    landing,
    submissions,
    task_save,
    uploads,
)
from forge.services.task_save import Draft, Published
from forge.testing import FakeClock, register_entrant
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    checked,
    organiser,
    signed_in,
    upload,
    write_contest,
)
from tests.test_run_task import (
    CARTPOLE_FILES,
    CARTPOLE_TASK,
    ISLANDS,
    TITLED,
    TOUR_FILES,
    TOUR_TASK,
    deployed,
)

DRAFT = RUNNING.format(visibility="everyone").replace("state: published", "state: draft")
"""acme/spring, not started, with no leaderboard."""

ISLANDS_DATA = {
    "samples/1.in": b"1\n",
    "samples/1.ans": b"1\n",
    "samples/2.in": b"2\n",
    "samples/2.ans": b"2\n",
    "tests/3.in": b"3\n",
    "tests/3.ans": b"3\n",
    "tests/4.in": b"4\n",
    "tests/4.ans": b"4\n",
}
TOUR_DATA = {"parts/1.in": b"5 cities\n", "parts/2.in": b"9 cities\n"}

TASKS: dict[str, tuple[str, Mapping[str, bytes], Mapping[str, bytes]]] = {
    "islands": (
        ISLANDS.replace("unicon/classic@v4", "unicon/islands@v1"),
        deployed("unicon/classic@v4")[1],
        ISLANDS_DATA,
    ),
    "cartpole": (
        CARTPOLE_TASK.replace("cs2109s/cartpole@v1", "unicon/cartpole@v1"),
        CARTPOLE_FILES,
        {},
    ),
    "tour": (TOUR_TASK.replace("acme/tour@v1", "unicon/tour@v1"), TOUR_FILES, TOUR_DATA),
}


async def _save(
    setup: Setup, acme: Acme, task: TaskId, contents: Mapping[str, bytes | None], **options: Any
) -> Published | Draft:
    head = await acme.fake.content.list_files(PLATFORM, task)
    return await task_save.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens.get(path)) for path, content in contents.items()},
        **options,
    )


async def _ready(acme: Acme, which: str, contest: str = DRAFT) -> str:
    """The workflow `which` names public at `v1` with its files, and the
    contest; the task's `task.yaml`.
    """
    text, held, _ = TASKS[which]
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, "unicon", which, dict(held), Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")
    await write_contest(acme.fake, contest)
    return text


async def _published(
    setup: Setup, acme: Acme, task: TaskId, which: str, contest: str = DRAFT, text: str = ""
) -> Published:
    """The task `which` saved over `task` and published, each question
    the save asks answered.
    """
    written = await _ready(acme, which, contest)
    contents = {
        "task.yaml": (text or written).encode(),
        "statement.md": b"Solve the task.\n",
        **TASKS[which][2],
    }
    try:
        result = await _save(setup, acme, task, contents, confirm=True)
    except ConfirmationRequired as asked:
        result = await _save(setup, acme, task, contents, confirm=True, asked=asked.extra["asked"])
    assert isinstance(result, Published), result
    await checked(setup, task)
    return result


def _runs_file(acme: Acme) -> RunsFile:
    return RunsFile.from_bytes(acme.fake.state.repos[("acme", "spring.sum.task")].files[RUNS_PATH])


def _plan(acme: Acme) -> RunPlan:
    return RunPlan.from_bytes(acme.fake.state.repos[("acme", "spring.sum.task")].files[PLAN_PATH])


def _errors(result: Published | Draft) -> list[tuple[str, str]]:
    assert isinstance(result, Draft), result
    return [(error["path"], error["message"]) for error in result.errors]


@pytest.mark.parametrize(
    ("which", "ids", "early"),
    [
        ("islands", ["r1", "r2", "r3", "r4"], {"r1", "r2"}),
        ("cartpole", ["r1", "r2", "r3", "r4", "r5", "r6"], {"r1", "r2", "r3", "r4", "r5", "r6"}),
        ("tour", ["r1", "r2"], {"r1", "r2"}),
    ],
)
async def test_the_three_tasks_publish_their_runs_and_plan(
    setup: Setup, acme: Acme, sum_task: TaskId, which: str, ids: list[str], early: set[str]
) -> None:
    result = await _published(setup, acme, sum_task, which)

    assert (result.number, result.grading_changed, result.changes, result.regraded) == (
        1,
        False,
        (),
        0,
    )
    runs = _runs_file(acme)
    assert [run.id for run in runs.runs] == ids
    assert {run.id for run in runs.runs if run.early} == early
    plan = _plan(acme)
    assert [(run.id, run.early) for run in plan.runs] == [(run.id, run.early) for run in runs.runs]
    # Each save resolves the version once, as the organiser, and reads
    # every file at that commit, which the plan pins.
    calls = acme.fake.calls_to("workflow_version")
    assert calls and {call.identity for call in calls} == {acme.ada.identity}
    commit = acme.fake.state.repos[("unicon", f"{which}.workflow")].versions["v1"]
    assert (plan.workflow.ref, plan.workflow.commit) == (f"unicon/{which}@v1", commit)
    reads = [*acme.fake.calls_to("read_workflow_at"), *acme.fake.calls_to("workflow_files")]
    assert reads and {read.arguments["at"] for read in reads} == {commit}
    # Every file a step is given is pinned by its hash too.
    saved = acme.fake.state.repos[("acme", "spring.sum.task")].files
    held = TASKS[which][1]
    assert plan.workflow.files
    assert {path: fact.sha256 for path, fact in plan.workflow.files.items()} == {
        path: hashlib.sha256(held[path]).hexdigest() for path in plan.workflow.files
    }
    assert {path: fact.sha256 for path, fact in plan.task_files.items()} == {
        path: hashlib.sha256(saved[path]).hexdigest() for path in plan.task_files
    }


async def test_a_saved_again_task_keeps_its_run_ids_and_publishes_only_what_is_new(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task, "tour")
    text = TASKS["tour"][0].replace(
        "      - [parts/2.in, entrant]\n",
        "      - [parts/2.in, entrant]\n      - [parts/3.in, entrant]\n",
    )

    unchanged = await _save(setup, acme, sum_task, {})
    added = await _save(
        setup, acme, sum_task, {"task.yaml": text.encode(), "parts/3.in": b"3 cities\n"}
    )

    assert isinstance(unchanged, Published) and unchanged.number == 1
    assert isinstance(added, Published) and added.number == 2
    assert (added.grading_changed, added.changes, added.regraded) == (
        True,
        ("The run r3 was added.", "The file parts/3.in was added."),
        0,
    )
    assert _plan(acme).task_files["parts/3.in"] == FileFact(
        size=len(b"3 cities\n"), sha256=hashlib.sha256(b"3 cities\n").hexdigest()
    )
    # The files the save does not write are read in one batch to hash them.
    assert any(len(call.arguments["tokens"]) == 2 for call in acme.fake.calls_to("read_blobs"))
    assert [(run.id, run.key["input"]) for run in _runs_file(acme).runs] == [
        ("r1", "parts/1.in"),
        ("r2", "parts/2.in"),
        ("r3", "parts/3.in"),
    ]


async def test_a_titles_only_save_publishes_and_regrades_nothing(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task, "tour")
    named = TITLED.replace("acme/tour@v1", "unicon/tour@v1").replace(", max_size: 50MB", "")

    saved = await _save(setup, acme, sum_task, {"task.yaml": named.encode(), **TOUR_DATA})

    assert isinstance(saved, Published) and saved.number == 2
    assert (saved.grading_changed, saved.changes, saved.regraded) == (False, (), 0)
    assert [run.titles for run in _plan(acme).runs] == [("Small grid",), ("Big grid",)]


async def test_a_part_over_its_own_size_is_refused_at_the_slot_and_at_submit(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    text = TITLED.replace("acme/tour@v1", "unicon/tour@v1")
    await _published(setup, acme, sum_task, "tour", RUNNING.format(visibility="everyone"))
    contents = {"task.yaml": text.encode(), **TOUR_DATA}
    try:
        saved = await _save(setup, acme, sum_task, contents, confirm=True)
    except ConfirmationRequired as asked:
        saved = await _save(
            setup, acme, sum_task, contents, confirm=True, asked=asked.extra["asked"]
        )
    assert isinstance(saved, Published)
    await checked(setup, sum_task)
    await register_entrant(setup, SPRING, 8)
    bob = await signed_in(setup, acme.fake, 8)
    digest = "ab" * 32

    with pytest.raises(TooLarge) as part_two:
        await uploads.slot(
            setup,
            bob,
            sum_task,
            input="route",
            filename="route.txt",
            size=60 * 1024 * 1024,
            sha256=digest,
            part=2,
        )
    with pytest.raises(TooLarge) as part_one:
        await uploads.slot(
            setup,
            bob,
            sum_task,
            input="route",
            filename="route.txt",
            size=20 * 1024 * 1024,
            sha256=digest,
            part=1,
        )
    assert (part_two.value.extra["limit"], part_two.value.extra["part"]) == (50 * 1024 * 1024, 2)
    assert (part_one.value.extra["limit"], part_one.value.extra["part"]) == (10 * 1024 * 1024, 1)

    large = await upload(
        setup, acme.fake, bob, sum_task, b"x" * 20, input="route", filename="route.txt"
    )
    # The bytes are small; the row's declared size is what the check reads,
    # so set it to 20MB, under the largest part and over part 1.
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Upload, large.id)
        assert row is not None
        row.size = 20 * 1024 * 1024
    with pytest.raises(TooLarge, match="part 1") as at_submit:
        await submissions.submit(
            setup,
            bob,
            sum_task,
            {"route": SubmittedInput(parts={1: SubmittedInput(uploads=(large.id,))})},
            idempotency_key="sized-part",
        )
    assert (at_submit.value.extra["input"], at_submit.value.extra["part"]) == ("route", 1)
    bigger = await upload(
        setup, acme.fake, bob, sum_task, b"y" * 20, input="route", filename="other.txt"
    )
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(Upload, bigger.id)
        assert row is not None
        row.size = 60 * 1024 * 1024
    with pytest.raises(TooLarge, match="part 2"):
        await submissions.submit(
            setup,
            bob,
            sum_task,
            {"route": SubmittedInput(parts={2: SubmittedInput(uploads=(bigger.id,))})},
            idempotency_key="sized-part-2",
        )


async def test_a_run_task_whose_runs_file_does_not_read_is_refused_not_renumbered(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _published(setup, acme, sum_task, "islands")

    async def unread(*_: object) -> None:
        return None

    monkeypatch.setattr(task_save.sources, "_runs_file_of", unread)
    result = await _save(setup, acme, sum_task, {"statement.md": b"Count the islands.\n"})

    assert _errors(result) == [("", task_save.RUNS_UNREAD)]
    assert len(acme.fake.calls_to("publish")) == 1


async def test_a_task_whose_scoring_is_empty_is_refused(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    run = await _ready(acme, "tour")

    empty = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": run.split("scoring:")[0].encode() + b"scoring:\n", **TOUR_DATA},
    )

    assert _errors(empty) == [("scoring", "This is required.")]
    assert acme.fake.calls_to("publish") == []


async def test_a_self_reported_report_asks_once_and_is_refused_until_confirmed(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    text = await _ready(acme, "cartpole")

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(setup, acme, sum_task, {"task.yaml": text.encode()})
    with pytest.raises(ConfirmationRequired):
        await _save(setup, acme, sum_task, {"task.yaml": text.encode()}, confirm=True, asked="x")
    # A bare confirmation, or one to the running contest's question alone,
    # does not answer what was asked.
    with pytest.raises(ConfirmationRequired):
        await _save(setup, acme, sum_task, {"task.yaml": text.encode()}, confirm=True)
    basis = asked.value.extra["asked"].rsplit(":", 1)[0]
    with pytest.raises(ConfirmationRequired):
        await _save(setup, acme, sum_task, {"task.yaml": text.encode()}, confirm=True, asked=basis)

    assert acme.fake.calls_to("publish") == []
    assert asked.value.detail == (
        "What the entrant gives as weights could set passed, reward and verdict, so they are "
        "self-reported, as are reward and windy, reading them: entrants could give themselves "
        "any result there. Confirm the task means it. Publish it, or keep it as a draft."
    )
    assert asked.value.extra["self_reported"] == {
        "passed": ["weights"],
        "reward": ["weights"],
        "verdict": ["weights"],
    }
    confirmed = await _save(
        setup,
        acme,
        sum_task,
        {"task.yaml": text.encode()},
        confirm=True,
        asked=asked.value.extra["asked"],
    )
    assert isinstance(confirmed, Published)
    # The next version marks the same, so it publishes without asking.
    again = await _save(setup, acme, sum_task, {"statement.md": b"Balance the pole.\n"})
    assert isinstance(again, Published) and again.number == 2


async def test_a_running_contest_and_a_self_reported_report_ask_as_one_question(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    text = await _ready(acme, "cartpole", RUNNING.format(visibility="everyone"))

    with pytest.raises(ConfirmationRequired) as asked:
        await _save(
            setup,
            acme,
            sum_task,
            {"task.yaml": text.encode(), "statement.md": b"Balance the pole.\n"},
        )

    assert asked.value.detail.startswith(
        "The contest is running, so this save asks before it publishes. What the entrant gives "
        "as weights could set"
    )
    assert asked.value.extra["runs_again"] == [
        "No submission runs again: this is the task's first publication."
    ]


async def test_what_was_shown_under_a_graded_publication_is_not_hidden_again(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = await _published(setup, acme, sum_task, "islands")

    async def graded(*_: object) -> set[str]:
        return {published.publication}

    monkeypatch.setattr(task_save.sources, "_graded_under", graded)
    text = TASKS["islands"][0]
    later = text.replace("now: [sample_score, sample_time,", "now: [sample_score,").replace(
        "after_close: [st1,", "after_close: [sample_time, st1,"
    )
    dropped = text.replace(", samples/]", "]")

    assert _errors(await _save(setup, acme, sum_task, {"task.yaml": later.encode()})) == [
        (
            "scoring.show.after_close",
            "Entrants have been shown sample_time as soon as a grading ends, and what they have "
            "been shown cannot be hidden again. List it as soon as before.",
        )
    ]
    assert _errors(await _save(setup, acme, sum_task, {"task.yaml": dropped.encode()})) == [
        (
            "scoring.show",
            "Entrants have been shown samples/ as soon as a grading ends, and what they have "
            "been shown cannot be hidden again. List it as soon as before.",
        )
    ]
    # The same files listed one by one show as much.
    each = text.replace("samples/]", "samples/1.in, samples/1.ans, samples/2.in, samples/2.ans]")
    listed = await _save(setup, acme, sum_task, {"task.yaml": each.encode()}, confirm=True)
    assert isinstance(listed, Published)


async def test_the_files_show_lists_are_served_from_their_time_and_no_other(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    text = TASKS["islands"][0].replace(
        "after_close: [st1, st2, total, all]", "after_close: [st1, st2, total, all, tests/3.in]"
    )
    await _published(setup, acme, sum_task, "islands", RUNNING.format(visibility="everyone"), text)
    await _save(setup, acme, sum_task, {"public/figure.png": b"figure"}, keep_as_draft=True)
    ada, bob = await signed_in(setup, acme.fake, 7), await signed_in(setup, acme.fake, 8)

    assert (await landing.public_file(setup, sum_task, "samples/1.in")).content == b"1\n"
    assert (await contest_home.public_file(setup, bob, sum_task, "samples/2.ans")).content == (
        b"2\n"
    )
    for path in ("tests/3.in", "tests/4.in", "task.yaml", "statement.md", RUNS_PATH):
        with pytest.raises(NotFound, match="no such file"):
            await landing.public_file(setup, sum_task, path)
    # The statement's preview draws what show lists, at any time, and
    # nothing else.
    assert (await contest_home.public_file(setup, ada, sum_task, "tests/3.in")).content == b"3\n"
    for path in ("tests/4.in", "public/figure.png"):
        with pytest.raises(NotFound, match="no such file"):
            await contest_home.public_file(setup, ada, sum_task, path)

    clock.advance(timedelta(hours=3, minutes=1))

    assert (await landing.public_file(setup, sum_task, "tests/3.in")).content == b"3\n"
    with pytest.raises(NotFound, match="no such file"):
        await landing.public_file(setup, sum_task, "tests/4.in")


async def test_after_close_is_served_from_the_reveal_and_on_release_never_yet(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    text = TASKS["islands"][0].replace(
        "after_close: [st1, st2, total, all]",
        "after_close: [st1, st2, total, all, tests/3.in]\n    on_release: [tests/4.in]",
    )
    await _published(setup, acme, sum_task, "islands", RUNNING.format(visibility="everyone"), text)
    reveal = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

    clock.set(reveal - timedelta(microseconds=1))
    for path in ("tests/3.in", "tests/4.in"):
        with pytest.raises(NotFound, match="no such file"):
            await landing.public_file(setup, sum_task, path)

    clock.set(reveal)
    assert (await landing.public_file(setup, sum_task, "tests/3.in")).content == b"3\n"
    clock.advance(timedelta(days=365))
    with pytest.raises(NotFound, match="no such file"):
        await landing.public_file(setup, sum_task, "tests/4.in")


LEADERBOARD = (
    "leaderboards:\n  standings:\n    columns:\n"
    "      score: {value: [{block: latest, from: {results: sum}}, total]}\n"
)


async def test_a_contest_gives_a_run_task_a_due_and_a_leaderboard_but_no_worth_or_late_share(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    # A stored contest still giving a worth or a late share reads with them
    # ignored, so the task saves; a save of the contest that has either is
    # refused.
    late = DRAFT + "    worth: 50\n    due: 2026-09-26T14:00:00Z\n    late_per_day: 0.5\n"
    text = await _ready(acme, "tour", late)
    contents = {"task.yaml": text.encode(), **TOUR_DATA}

    assert isinstance(await _save(setup, acme, sum_task, contents, confirm=True), Published)
    await write_contest(acme.fake, DRAFT + LEADERBOARD)
    assert isinstance(await _save(setup, acme, sum_task, contents, confirm=True), Published)
    await checked(setup, sum_task)

    current = await files.read(setup, acme.ada, SPRING, "contest.yaml")
    for content, path in (
        (DRAFT + "    worth: 50\n", "tasks[0].worth"),
        (DRAFT + "leaderboards:\n  - {name: Standings}\n", "leaderboards"),
        (DRAFT + "    due: 2026-09-26T14:00:00Z\n    late_per_day: 0.5\n", "tasks[0].late_per_day"),
    ):
        with pytest.raises(InvalidDefinition) as refused:
            await files.write(
                setup, acme.ada, SPRING, "contest.yaml", content.encode(), current.token
            )
        assert [error["path"] for error in refused.value.errors] == [path]
    # seconds_late reads a due, so a task may have one.
    due = DRAFT + "    due: 2026-09-26T14:00:00Z\n"
    assert await files.write(setup, acme.ada, SPRING, "contest.yaml", due.encode(), current.token)


async def test_an_entrant_submits_to_a_run_task_and_its_early_row_is_queued(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task, "tour", RUNNING.format(visibility="everyone"))
    await register_entrant(setup, SPRING, 8)
    bob = await signed_in(setup, acme.fake, 8)
    first, second = [
        await upload(setup, acme.fake, bob, sum_task, route, input="route", filename="route.txt")
        for route in (b"1 2 3\n", b"3 2 1\n")
    ]
    with pytest.raises(InvalidInputs, match="route"):
        await submissions.submit(
            setup,
            bob,
            sum_task,
            {"route": SubmittedInput(uploads=(first.id,))},
            idempotency_key="run-task-0",
        )

    given = SubmittedInput(
        parts={1: SubmittedInput(uploads=(first.id,)), 2: SubmittedInput(uploads=(second.id,))}
    )
    made = await submissions.submit(
        setup, bob, sum_task, {"route": given}, idempotency_key="run-task-1"
    )

    page = await contest_home.task(setup, bob, sum_task)
    assert [field.id for field in page.inputs] == ["route"]
    async with setup.unit_of_work() as ctx:
        rows = (await ctx.db.scalars(select(Grading).where(Grading.task_id == sum_task))).all()
    assert [(row.pipeline, row.submission_number) for row in rows if row.pipeline != "save"] == [
        ("early", made.number)
    ]


async def test_a_part_file_is_downloaded_and_a_recovered_parts_submission_uses_its_uploads(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    await _published(setup, acme, sum_task, "tour", RUNNING.format(visibility="everyone"))
    entrant = await register_entrant(setup, SPRING, 8)
    bob = await signed_in(setup, acme.fake, 8)
    first, second = [
        await upload(setup, acme.fake, bob, sum_task, route, input="route", filename="route.txt")
        for route in (b"1 2 3\n", b"3 2 1\n")
    ]
    given = {
        "route": SubmittedInput(
            parts={1: SubmittedInput(uploads=(first.id,)), 2: SubmittedInput(uploads=(second.id,))}
        )
    }
    acme.fake.lose_submission_answer = True
    with pytest.raises(Unavailable):
        await submissions.submit(setup, bob, sum_task, given, idempotency_key="parts-lost")

    made = await submissions.submit(setup, bob, sum_task, given, idempotency_key="parts-lost")

    async with setup.unit_of_work() as ctx:
        rows = (await ctx.db.scalars(select(Upload))).all()
    assert {row.id: row.status for row in rows} == {first.id: "consumed", second.id: "consumed"}
    door = await submissions.download(setup, bob, sum_task, made.number, "files/route/1/route.txt")
    assert await acme.fake.workspaces.fetch(door) == b"1 2 3\n"
    acme.fake.add_user(41, "eve")
    await acme.fake.orgs.grant_role(41, Scope("acme", "spring", "sum"), Role.OBSERVER)
    eve = await organiser(setup, acme.fake, 41, Scope("acme", "spring", "sum"))
    staff = await submissions.organised_download(
        setup, eve, sum_task, EntrantOwner(entrant), made.number, "files/route/2/route.txt"
    )
    assert await acme.fake.workspaces.fetch(staff) == b"3 2 1\n"


async def test_asking_before_a_save_counts_the_regrade_its_check_waits_for(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _published(setup, acme, sum_task, "tour", RUNNING.format(visibility="everyone"))
    text = TASKS["tour"][0].replace(
        "      - [parts/2.in, entrant]\n",
        "      - [parts/2.in, entrant]\n      - [parts/3.in, entrant]\n",
    )

    async def five(*_: object, **__: object) -> int:
        return 5

    monkeypatch.setattr(gradings, "to_regrade", five)
    with pytest.raises(ConfirmationRequired) as asked:
        await _save(
            setup, acme, sum_task, {"task.yaml": text.encode(), "parts/3.in": b"3 cities\n"}
        )

    assert asked.value.extra["changes"] == [
        "The run r3 was added.",
        "The file parts/3.in was added.",
    ]
    assert asked.value.extra["regrades"] == 5
    # Graded again once the save row passes, as the take-over will choose.
    assert asked.value.extra["runs_again"] == [
        "Once the save's check passes, 5 submissions are graded again, running check again, "
        "for these changes:",
        "The run r3 was added.",
        "The file parts/3.in was added.",
    ]
