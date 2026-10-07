"""A contestant's uploads become a submission. A passing submit is one commit
by the contestant with the files under `files/` and `submission.json`
(version 5), named `submission/<n>` by the platform, and one queued grading
row against the current publication, holding the hash of its callback token.
Each refusal comes before anything is written, with its own code, in the
order the rules give. A number another took first is taken by the next. The
same key sent twice returns the first submission and makes nothing, and a
submit whose forge writes landed but whose rows did not is completed by the
retry instead of made again. A folder input takes a tree of files and a
per-test input one file per test, named `<group>/<test>`, the files under an
input within its `max_size` together. The contestant reads their own
submissions back, each grading with its test groups as their `show` allows
before and after the task's reveal, with the publication it ran under when
the tests have changed since, a run in `system_error` as still running, how
many days late each was, and nobody else's.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from forge.db.tables import Grading
from forge.db.tables import Upload as UploadRow
from forge.domain.content import Edit
from forge.domain.definitions import Show
from forge.domain.errors import (
    Archived,
    InvalidIdempotencyKey,
    InvalidInputs,
    NotApproved,
    NotFound,
    RateLimited,
    Rejected,
    SubmissionLimit,
    TaskClosed,
    TooLarge,
    Unavailable,
    UploadNotReady,
    UploadNotYours,
)
from forge.domain.grading import GradingStatus, callback_token, token_hash
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId, TaskId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.scoring import Points
from forge.domain.showing import GroupShown
from forge.domain.submissions import SubmittedInput
from forge.domain.uploads import pointer_text
from forge.domain.workflows import Visibility
from forge.port.uploads import SubmissionPlace
from forge.runtime.setup import Setup
from forge.services import contestants, identity, publications, submissions, uploads
from forge.testing import CLASSIC, FakeClock
from tests.services.conftest import (
    RUNNING,
    SPRING,
    Acme,
    Entered,
    organiser,
    signed_in,
    upload,
    write_contest,
)

KEY = "key-0001-aaaa"
SOURCE = b"print(sum(map(int, input().split())))\n"
END = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

FOLDERS = CLASSIC.replace(
    b"submission: {type: file, contestant: true}", b"submission: {type: folder, contestant: true}"
)
"""classic, with the contestant's sources a folder."""

ANSWERS = b"""\
inputs:
  answers: {type: file, contestant: true, per_test: true}
test:
  input: file
  answer: file
steps:
  - id: check
    use: unicon/diff-check@v2
    per_test: true
    with:
      actual: ${{ inputs.answers }}
      expected: ${{ test.answer }}
"""
"""An output-only workflow: the contestant uploads one answer per test."""


def code(*uploads: Any, language: str = "python") -> dict[str, SubmittedInput]:
    return {
        "submission": SubmittedInput(uploads=tuple(uploads)),
        "language": SubmittedInput(value=language),
    }


async def _gradings(setup: Setup) -> list[Grading]:
    async with setup.unit_of_work() as ctx:
        return list(
            (await ctx.db.execute(select(Grading).order_by(Grading.submission_number))).scalars()
        )


async def _submit(
    setup: Setup, acme: Acme, entered: Entered, key: str = KEY, content: bytes = SOURCE
) -> submissions.Submission:
    made = await upload(setup, acme.fake, entered.session, entered.task, content)
    return await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=key
    )


async def _save(setup: Setup, acme: Acme, task: TaskId, files: Mapping[str, bytes]) -> None:
    """The task's files saved by ada and published, confirmed while the
    contest runs.
    """
    head = await acme.fake.content.list_files(PLATFORM, task)
    saved = await publications.save(
        setup,
        acme.ada,
        task,
        {path: Edit(content, head.tokens.get(path)) for path, content in files.items()},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved


async def _task_yaml(acme: Acme, task: TaskId) -> bytes:
    return (await acme.fake.content.read_file(PLATFORM, task, "task.yaml")).content


async def _workflow(acme: Acme, name: str, source: bytes) -> None:
    """`acme/<name>@v1`, public at the fake."""
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, "acme", name, {"workflow.yaml": source}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")


async def _snapshot(acme: Acme, number: int) -> dict[str, bytes]:
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    return dict(repo.snapshots[repo.versions[f"submission/{number}"]])


def _pointer(content: bytes) -> bytes:
    return pointer_text(hashlib.sha256(content).hexdigest(), len(content))


async def test_a_submit_commits_as_the_contestant_names_it_and_queues_one_grading(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    submission = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )

    assert (submission.task, submission.number, submission.submitted_at) == (
        entered.task,
        1,
        clock.now(),
    )
    assert submission.late_days == 0
    assert submission.grading is not None
    assert (submission.grading.attempt, submission.grading.status) == (1, GradingStatus.QUEUED)
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    head = repo.history[-1]
    assert head.author_id == 8
    assert repo.versions == {"submission/1": head.version}
    assert sorted(repo.snapshots[head.version]) == ["files/submission/main.py", "submission.json"]
    assert repo.snapshots[head.version]["files/submission/main.py"] == _pointer(SOURCE)
    assert json.loads(repo.snapshots[head.version]["submission.json"]) == {
        "schema_version": 5,
        "inputs": {
            "submission": {"files": ["files/submission/main.py"]},
            "language": {"value": "python"},
        },
    }
    [recorded] = acme.fake.calls_to("record_submission")
    assert recorded.identity.user_id == 8  # type: ignore[union-attr]

    [row] = await _gradings(setup)
    [publication] = await acme.fake.workspaces.list_publications(entered.task)
    assert (row.task_id, row.workspace_id, row.submission_id) == (
        entered.task,
        "acme/spring/@u8",
        "acme/spring/@u8/sum#1",
    )
    assert (row.submission_number, row.submission_version, row.publication_id) == (
        1,
        head.version,
        publication.id,
    )
    assert (row.id, row.attempt, row.status, row.idempotency_key, row.result) == (
        submission.grading.id,
        1,
        "dispatched",
        KEY,
        None,
    )
    assert len(acme.fake.calls_to("start_run")) == 1
    token = callback_token(setup.settings.token_encryption_key_bytes, row.id)
    assert row.callback_token_hash == token_hash(token)
    async with setup.unit_of_work() as ctx:
        used = (await ctx.db.execute(select(UploadRow))).scalar_one()
    assert (used.status, used.consumed_by) == ("consumed", "acme/spring/@u8/sum#1")


async def test_the_place_to_submit_is_made_once_at_the_first_slot(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # An object belongs to a repository, so the place is made before any
    # bytes are sent rather than at the submit; the files of one submission
    # cost one call between them, not one each.
    assert ("acme", "spring.sum.u8.sub") not in acme.fake.state.repos

    await _submit(setup, acme, entered)
    clock.advance(timedelta(seconds=31))
    await _submit(setup, acme, entered, key="key-0002-bbbb")

    # Two submissions of one file each: the first slot makes the place and
    # nothing after it asks again.
    (opened,) = acme.fake.calls_to("open_submission_place")
    assert opened.arguments["member_ids"] == [8]
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    assert sorted(repo.versions) == ["submission/1", "submission/2"]


async def test_a_second_submission_holds_only_its_own_files(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    clock.advance(timedelta(seconds=31))
    other = await upload(setup, acme.fake, entered.session, entered.task, b"x\n", filename="b.py")

    second = await submissions.submit(
        setup, entered.session, entered.task, code(other.id), idempotency_key="key-0002-bbbb"
    )

    assert second.number == 2
    assert sorted(await _snapshot(acme, 2)) == ["files/submission/b.py", "submission.json"]
    assert [row.submission_number for row in await _gradings(setup)] == [1, 2]


async def test_a_number_another_took_first_is_retried_and_the_row_points_at_the_new_one(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.racing_submissions = 1

    submission = await _submit(setup, acme, entered)

    assert submission.number == 2
    [row] = await _gradings(setup)
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    assert (row.submission_id, row.submission_version) == (
        "acme/spring/@u8/sum#2",
        repo.versions["submission/2"],
    )


async def test_the_same_key_twice_returns_the_first_submission_and_makes_nothing(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    first = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )

    again = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )

    assert (again.number, again.submitted_at) == (first.number, first.submitted_at)
    assert again.grading is not None and first.grading is not None
    assert again.grading.id == first.grading.id
    assert len(acme.fake.calls_to("record_submission")) == 1
    assert len(await _gradings(setup)) == 1
    assert len(acme.fake.state.repos[("acme", "spring.sum.u8.sub")].versions) == 1


async def test_a_retry_after_the_answer_was_lost_finds_the_submission_and_adds_its_row(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    acme.fake.lose_submission_answer = True
    with pytest.raises(Unavailable):
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )
    assert await _gradings(setup) == []

    again = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )

    assert again.number == 1
    assert len(acme.fake.calls_to("record_submission")) == 1
    [row] = await _gradings(setup)
    assert (row.submission_id, row.idempotency_key, row.status) == (
        "acme/spring/@u8/sum#1",
        KEY,
        "dispatched",
    )


async def _statuses(setup: Setup) -> dict[str, tuple[str, str | None]]:
    async with setup.unit_of_work() as ctx:
        rows = (await ctx.db.execute(select(UploadRow))).scalars()
        return {str(row.id): (row.status, row.consumed_by) for row in rows}


async def _lost(setup: Setup, acme: Acme, entered: Entered, *named: Any) -> None:
    """A submit of `named` whose submission lands at the forge and whose rows
    do not."""
    acme.fake.lose_submission_answer = True
    with pytest.raises(Unavailable):
        await submissions.submit(
            setup, entered.session, entered.task, code(*named), idempotency_key=KEY
        )
    assert await _gradings(setup) == []


async def test_a_recovered_submission_consumes_the_uploads_its_own_files_point_at(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    used = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await _lost(setup, acme, entered, used.id)
    assert (await _statuses(setup))[str(used.id)] == ("verified", None)
    # The retry carries the same key and another file: what the submission
    # used is read from the submission, never from the retry.
    other = await upload(setup, acme.fake, entered.session, entered.task, b"x\n", filename="b.py")

    again = await submissions.submit(
        setup, entered.session, entered.task, code(other.id), idempotency_key=KEY
    )

    assert again.number == 1
    assert await _statuses(setup) == {
        str(used.id): ("consumed", "acme/spring/@u8/sum#1"),
        str(other.id): ("verified", None),
    }


async def test_of_two_uploads_of_one_file_a_recovered_submission_consumes_the_oldest(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    first = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await _lost(setup, acme, entered, first.id)
    clock.advance(timedelta(seconds=1))
    second = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    await submissions.submit(setup, entered.session, entered.task, code(), idempotency_key=KEY)

    assert await _statuses(setup) == {
        str(first.id): ("consumed", "acme/spring/@u8/sum#1"),
        str(second.id): ("verified", None),
    }
    clock.advance(timedelta(seconds=31))
    later = await submissions.submit(
        setup, entered.session, entered.task, code(second.id), idempotency_key="key-0002-bbbb"
    )
    assert later.number == 2


async def test_a_later_submit_cannot_name_an_upload_an_unrecorded_submission_used(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    used = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await _lost(setup, acme, entered, used.id)
    clock.advance(timedelta(seconds=31))

    # A new key finds no submission of its own, but the one at the forge
    # without rows is finished first, so its upload is taken.
    with pytest.raises(UploadNotReady):
        await submissions.submit(
            setup, entered.session, entered.task, code(used.id), idempotency_key="key-0002-bbbb"
        )
    assert len(acme.fake.calls_to("record_submission")) == 1

    # The refusal rolled the finishing back with it; the next submit that
    # passes finishes it for good, beside its own.
    other = await upload(setup, acme.fake, entered.session, entered.task, b"x\n", filename="b.py")
    later = await submissions.submit(
        setup, entered.session, entered.task, code(other.id), idempotency_key="key-0003-cccc"
    )

    assert later.number == 2
    rows = await _gradings(setup)
    assert [(row.submission_number, row.idempotency_key) for row in rows] == [
        (1, KEY),
        (2, "key-0003-cccc"),
    ]
    assert await _statuses(setup) == {
        str(used.id): ("consumed", "acme/spring/@u8/sum#1"),
        str(other.id): ("consumed", "acme/spring/@u8/sum#2"),
    }
    mine = await submissions.mine(setup, entered.session, entered.task)
    assert [made.number for made in mine] == [2, 1]


async def test_a_retry_whose_rows_carry_no_key_finds_them_by_the_submission(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(idempotency_key=None))

    again = await submissions.submit(
        setup, entered.session, entered.task, code(), idempotency_key=KEY
    )

    assert again.number == 1
    assert len(await _gradings(setup)) == 1


async def test_a_submit_needs_a_valid_idempotency_key(setup: Setup, entered: Entered) -> None:
    with pytest.raises(InvalidIdempotencyKey):
        await submissions.submit(
            setup, entered.session, entered.task, code(), idempotency_key="short"
        )


async def test_a_closed_or_archived_task_is_refused_first(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    # The task closes at 11:00, before the clock's noon.
    await write_contest(
        acme.fake, RUNNING.format(visibility="everyone") + "    closes: 2026-09-26T11:00:00Z\n"
    )
    with pytest.raises(TaskClosed) as closed:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )
    assert closed.value.extra["reason"] == "closed"

    await write_contest(
        acme.fake, RUNNING.format(visibility="everyone").replace("published", "archived")
    )
    with pytest.raises(Archived):
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )

    # The task closes at the contest's end, 15:00, and bob's extension on it
    # moves his close to 16:00. It is given before the close: once the task
    # has revealed, no extension is.
    await write_contest(acme.fake, RUNNING.format(visibility="everyone"))
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(hours=1), tasks=["sum"])
    clock.advance(timedelta(hours=3, minutes=30))
    await identity.current(entered.session.id, setup=setup)
    passed = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )
    assert passed.number == 1
    assert len(acme.fake.calls_to("record_submission")) == 1

    clock.advance(timedelta(minutes=30))
    await identity.current(entered.session.id, setup=setup)
    with pytest.raises(TaskClosed) as ended:
        await submissions.submit(
            setup, entered.session, entered.task, code(), idempotency_key="key-0002-bbbb"
        )
    assert ended.value.extra["reason"] == "closed"


async def test_someone_not_approved_is_refused(setup: Setup, acme: Acme, entered: Entered) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    await contestants.register(setup, cyd, SPRING)
    with pytest.raises(NotApproved):
        await submissions.submit(setup, cyd, entered.task, code(), idempotency_key=KEY)
    assert acme.fake.calls_to("record_submission") == []
    assert acme.fake.calls_to("open_submission_place") == []


async def test_the_count_and_rate_limits_hold(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    current = await _task_yaml(acme, entered.task)
    await _save(
        setup,
        acme,
        entered.task,
        {"task.yaml": current + b"submissions:\n  max: 2\n  rate: {count: 1, per: 60}\n"},
    )
    await _submit(setup, acme, entered)
    clock.advance(timedelta(seconds=30))

    with pytest.raises(RateLimited) as limited:
        await _submit(setup, acme, entered, key="key-0002-bbbb")
    assert limited.value.extra["rate"] == "1 per 60s"
    assert limited.value.extra["retry_at"] == (clock.now() + timedelta(seconds=30)).isoformat()

    clock.advance(timedelta(seconds=30))
    await _submit(setup, acme, entered, key="key-0003-cccc")
    clock.advance(timedelta(minutes=5))
    with pytest.raises(SubmissionLimit) as spent:
        await _submit(setup, acme, entered, key="key-0004-dddd")
    assert spent.value.extra["limit"] == 2
    assert len(acme.fake.calls_to("record_submission")) == 2


async def test_uploads_must_be_the_contestants_own_and_ready(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    await contestants.register(setup, cyd, SPRING)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.approve(setup, manager, SPRING, 30)
    theirs = await upload(setup, acme.fake, cyd, entered.task, SOURCE)

    with pytest.raises(UploadNotYours) as foreign:
        await submissions.submit(
            setup, entered.session, entered.task, code(theirs.id), idempotency_key=KEY
        )
    assert foreign.value.extra["uploads"] == [str(theirs.id)]

    mine = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await submissions.submit(
        setup, entered.session, entered.task, code(mine.id), idempotency_key=KEY
    )
    clock.advance(timedelta(seconds=31))
    with pytest.raises(UploadNotReady):
        await submissions.submit(
            setup, entered.session, entered.task, code(mine.id), idempotency_key="key-0002-bbbb"
        )


async def test_a_file_over_the_size_its_input_takes_now_is_refused_naming_the_input(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, b"x" * 1000)
    current = await _task_yaml(acme, entered.task)
    smaller = current.replace(
        b"submission: {label: Your solution}", b"submission: {label: Your solution, max_size: 512B}"
    )
    assert smaller != current
    await _save(setup, acme, entered.task, {"task.yaml": smaller})

    with pytest.raises(TooLarge) as refused:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )

    assert (refused.value.extra["limit"], refused.value.extra["input"]) == (512, "submission")
    assert acme.fake.calls_to("record_submission") == []


@pytest.mark.parametrize(
    ("given", "input", "message"),
    [
        ({"language": SubmittedInput(value="rust")}, "language", "Choose one of python."),
        ({"language": SubmittedInput()}, "language", "This input is required."),
        ({"time_limit": SubmittedInput(value=5)}, "time_limit", "The task has no such input."),
        ({"language": SubmittedInput(value=2)}, "language", "Choose one of python."),
    ],
)
async def test_what_does_not_fit_the_inputs_is_refused_and_nothing_is_written(
    setup: Setup,
    acme: Acme,
    entered: Entered,
    given: dict[str, SubmittedInput],
    input: str,
    message: str,
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    with pytest.raises(InvalidInputs) as refused:
        await submissions.submit(
            setup,
            entered.session,
            entered.task,
            {**code(made.id), **given},
            idempotency_key=KEY,
        )

    assert {"input": input, "message": message} in refused.value.extra["errors"]
    assert acme.fake.calls_to("record_submission") == []
    assert await _gradings(setup) == []


async def test_a_file_input_takes_one_file_and_no_value(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    one = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    two = await upload(setup, acme.fake, entered.session, entered.task, b"x\n", filename="b.py")

    with pytest.raises(InvalidInputs) as many:
        await submissions.submit(
            setup, entered.session, entered.task, code(one.id, two.id), idempotency_key=KEY
        )
    with pytest.raises(InvalidInputs) as valued:
        await submissions.submit(
            setup,
            entered.session,
            entered.task,
            {**code(), "submission": SubmittedInput(value="print(1)")},
            idempotency_key=KEY,
        )

    assert many.value.extra["errors"] == [
        {"input": "submission", "message": "This input takes exactly one file."}
    ]
    assert valued.value.extra["errors"] == [
        {"input": "submission", "message": "This input needs a file."}
    ]
    assert acme.fake.calls_to("record_submission") == []


async def test_a_folder_input_takes_a_tree_of_files_within_its_size_together(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _workflow(acme, "folders", FOLDERS)
    current = await _task_yaml(acme, entered.task)
    await _save(
        setup,
        acme,
        entered.task,
        {
            "task.yaml": current.replace(b"unicon/classic@v2", b"acme/folders@v1").replace(
                b"{label: Your solution}", b"{label: Your sources, max_size: 1KB}"
            )
        },
    )
    session, task = entered.session, entered.task
    main = await upload(setup, acme.fake, session, task, b"import lib.add\n", filename="main.py")
    nested = await upload(
        setup, acme.fake, session, task, b"def add(a, b): ...\n", filename="lib/add.py"
    )

    made = await submissions.submit(
        setup, session, task, code(nested.id, main.id), idempotency_key=KEY
    )

    snapshot = await _snapshot(acme, made.number)
    assert sorted(snapshot) == [
        "files/submission/lib/add.py",
        "files/submission/main.py",
        "submission.json",
    ]
    assert snapshot["files/submission/lib/add.py"] == _pointer(b"def add(a, b): ...\n")
    assert json.loads(snapshot["submission.json"]) == {
        "schema_version": 5,
        "inputs": {
            "submission": {"files": ["files/submission/lib/add.py", "files/submission/main.py"]},
            "language": {"value": "python"},
        },
    }
    files = await submissions.files(setup, session, task, made.number)
    assert files.inputs["submission"] == {
        "files": ["files/submission/lib/add.py", "files/submission/main.py"]
    }

    # Each file fits on its own; the two of them together do not.
    clock.advance(timedelta(seconds=31))
    big = [
        await upload(setup, acme.fake, session, task, bytes([65 + n]) * 600, filename=f"{n}.py")
        for n in range(2)
    ]
    with pytest.raises(InvalidInputs) as refused:
        await submissions.submit(
            setup, session, task, code(*(made.id for made in big)), idempotency_key="key-0002-bbbb"
        )
    assert refused.value.extra["errors"] == [
        {
            "input": "submission",
            "message": "The files of this input total more than the 1024 bytes allowed.",
        }
    ]
    with pytest.raises(InvalidInputs) as empty:
        await submissions.submit(setup, session, task, code(), idempotency_key="key-0003-cccc")
    assert empty.value.extra["errors"] == [
        {"input": "submission", "message": "This input needs at least one file."}
    ]
    assert len(acme.fake.calls_to("record_submission")) == 1


async def test_a_per_test_input_takes_one_file_named_for_each_test(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _workflow(acme, "answers", ANSWERS)
    await _save(
        setup,
        acme,
        entered.task,
        {
            "task.yaml": b"name: Sum\nworkflow: acme/answers@v1\n"
            + b"test_groups:\n  main: {each: 100}\n",
            "tests/main/2/input": b"2 2\n",
            "tests/main/2/answer": b"4\n",
        },
    )
    session, task = entered.session, entered.task
    digest = hashlib.sha256(b"3\n").hexdigest()

    # The slot takes only a test's path, with or without an ending.
    for wrong in ("1.txt", "main/3.txt", "other/1", "main/1/x"):
        with pytest.raises(InvalidInputs):
            await uploads.slot(
                setup, session, task, input="answers", filename=wrong, size=2, sha256=digest
            )
    first = await upload(
        setup, acme.fake, session, task, b"3\n", input="answers", filename="main/1"
    )
    second = await upload(
        setup, acme.fake, session, task, b"4\n", input="answers", filename="main/2.txt"
    )

    made = await submissions.submit(
        setup,
        session,
        task,
        {"answers": SubmittedInput(uploads=(second.id, first.id))},
        idempotency_key=KEY,
    )

    snapshot = await _snapshot(acme, made.number)
    assert sorted(snapshot) == [
        "files/answers/main/1",
        "files/answers/main/2.txt",
        "submission.json",
    ]
    assert json.loads(snapshot["submission.json"]) == {
        "schema_version": 5,
        "inputs": {"answers": {"files": ["files/answers/main/1", "files/answers/main/2.txt"]}},
    }

    # A test may go unanswered, but never answered twice.
    clock.advance(timedelta(seconds=31))
    again = await upload(
        setup, acme.fake, session, task, b"3\n", input="answers", filename="main/1.out"
    )
    alone = await upload(
        setup, acme.fake, session, task, b"5\n", input="answers", filename="main/1"
    )
    with pytest.raises(InvalidInputs) as twice:
        await submissions.submit(
            setup,
            session,
            task,
            {"answers": SubmittedInput(uploads=(again.id, alone.id))},
            idempotency_key="key-0002-bbbb",
        )
    assert twice.value.extra["errors"] == [
        {"input": "answers", "message": "Two files answer the same test."}
    ]
    one = await submissions.submit(
        setup,
        session,
        task,
        {"answers": SubmittedInput(uploads=(alone.id,))},
        idempotency_key="key-0003-cccc",
    )
    assert sorted(await _snapshot(acme, one.number)) == ["files/answers/main/1", "submission.json"]


async def test_an_upload_the_forge_no_longer_holds_is_refused_before_the_commit(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # The forge collects an object no commit points at. A submit naming one
    # that has gone must refuse rather than commit a pointer to nothing, so
    # it asks the forge once more at the last moment.
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    workspace = acme.fake.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))
    place = SubmissionPlace(workspace, entered.task)
    acme.fake.uploads.forget(place, hashlib.sha256(SOURCE).hexdigest(), len(SOURCE))

    with pytest.raises(UploadNotReady) as error:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )

    assert error.value.extra == {"uploads": [str(made.id)]}
    assert acme.fake.calls_to("record_submission") == []


async def test_the_commit_holds_a_pointer_and_never_the_bytes(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )

    written = await _snapshot(acme, 1)
    path = next(name for name in written if name.startswith("files/"))
    assert written[path] == _pointer(SOURCE)
    assert SOURCE not in written[path]


async def test_a_failing_forge_or_store_is_told_in_fixed_words_and_nothing_is_written(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("/api/v1/repos/acme/spring.sum.u8.sub/contents answered 422")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the store answered 503")

    with monkeypatch.context() as patched:
        patched.setattr(acme.fake.workspaces, "record_submission", refused)
        with pytest.raises(Rejected) as failed:
            await submissions.submit(
                setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
            )
    assert failed.value.detail == submissions.FORGE_REFUSED
    with monkeypatch.context() as patched:
        patched.setattr(acme.fake.uploads, "holds", down)
        with pytest.raises(Unavailable) as unread:
            await submissions.submit(
                setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
            )
    assert unread.value.detail == uploads.FORGE_UNAVAILABLE
    assert await _gradings(setup) == []
    async with setup.unit_of_work() as ctx:
        [row] = (await ctx.db.execute(select(UploadRow))).scalars()
        assert row.status == "verified"

    await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )
    assert len(await _gradings(setup)) == 1


async def test_a_contestant_reads_their_own_submissions_and_their_files(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    clock.advance(timedelta(minutes=1))
    await _submit(setup, acme, entered, key="key-0002-bbbb", content=b"print(2)\n")

    listed = await submissions.mine(setup, entered.session, entered.task)

    assert [submission.number for submission in listed] == [2, 1]
    assert [submission.grading.attempt for submission in listed if submission.grading] == [1, 1]
    assert await submissions.one(setup, entered.session, entered.task, 1) == listed[1]
    files = await submissions.files(setup, entered.session, entered.task, 2)
    assert files.inputs == {
        "submission": {"files": ["files/submission/main.py"]},
        "language": {"value": "python"},
    }
    door = await submissions.download(
        setup, entered.session, entered.task, 2, "files/submission/main.py"
    )
    assert door.path.endswith("/media/files/submission/main.py?ref=submission%2F2")
    assert await acme.fake.workspaces.fetch(door) == b"print(2)\n"
    with pytest.raises(NotFound):
        await submissions.download(setup, entered.session, entered.task, 2, "submission.json")
    with pytest.raises(NotFound):
        await submissions.one(setup, entered.session, entered.task, 3)

    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    assert await submissions.mine(setup, cyd, entered.task) == ()
    with pytest.raises(NotFound):
        await submissions.one(setup, cyd, entered.task, 1)
    with pytest.raises(NotFound):
        await submissions.files(setup, cyd, entered.task, 1)
    with pytest.raises(NotFound):
        await submissions.download(setup, cyd, entered.task, 1, "files/submission/main.py")


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
    "values": {"log": "compiled"},
    "run_log": "http://machines.test/unicon-results/logs/x/1.log",
    "error": None,
}


async def _three_groups(setup: Setup, acme: Acme, task: TaskId) -> None:
    """The task with a group of each `show`, one test in each."""
    current = await _task_yaml(acme, task)
    start = current.index(b"test_groups:")
    files = {"task.yaml": current[:start] + GROUPS}
    for group in ("small", "large"):
        files[f"tests/{group}/1/input"] = b"1 1\n"
        files[f"tests/{group}/1/answer"] = b"2\n"
    await _save(setup, acme, task, files)


def _credited(index: int) -> dict[str, Any]:
    """RESULT's test row as shown, with the credit it earned."""
    row = RESULT["tests"][index]
    return {**row, "credit": 1 if row["outcome"] == "accepted" else 0}


async def _graded(setup: Setup, result: dict[str, Any], status: str = "done") -> None:
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(status=status, result=result))


async def test_each_group_is_shown_as_its_show_allows_until_the_task_reveals(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _three_groups(setup, acme, entered.task)
    await _submit(setup, acme, entered)
    await _graded(setup, RESULT)

    [before] = await submissions.mine(setup, entered.session, entered.task)

    assert before.grading is not None
    assert (before.grading.status, before.grading.stopped) == (GradingStatus.DONE, None)
    # The groups whose verdict is shown passed; large's verdict waits.
    assert before.grading.outcome == "accepted"
    assert before.grading.values == {"log": "compiled"}
    # Worth 100 over rule weights 100, 30 and 70: main 50, small 15 and
    # large's 35 decided at the reveal.
    assert before.grading.groups == (
        GroupShown(
            group="main",
            show=Show.ALWAYS,
            outcome="accepted",
            tests=(
                {
                    "test": "main/1",
                    "outcome": "accepted",
                    "values": {"time_ms": 10, "memory_kb": 7},
                    "credit": 1,
                },
            ),
            shown_at=None,
            points=Fraction(50),
            max=Fraction(50),
        ),
        GroupShown(
            group="small",
            show=Show.VERDICT,
            outcome="accepted",
            tests=None,
            shown_at=END,
            points=Fraction(15),
            max=Fraction(15),
        ),
        GroupShown(
            group="large",
            show=Show.AFTER_CLOSE,
            outcome=None,
            tests=None,
            shown_at=END,
            max=Fraction(35),
        ),
    )
    assert before.grading.points == Points(
        shown=Fraction(65), pending=Fraction(35), pending_until=END
    )

    clock.set(END)
    await identity.current(entered.session.id, setup=setup)
    [after] = await submissions.mine(setup, entered.session, entered.task)

    assert after.grading is not None
    assert after.grading.outcome == "wrong_answer"
    assert [(group.group, group.outcome, group.shown_at) for group in after.grading.groups] == [
        ("main", "accepted", None),
        ("small", "accepted", None),
        ("large", "wrong_answer", None),
    ]
    assert after.grading.groups[2].tests == (_credited(0),)
    assert after.grading.groups[1].tests == (_credited(2),)
    assert after.grading.points == Points(
        shown=Fraction(65), pending=Fraction(0), pending_until=None
    )
    assert after.grading.groups[2].points == 0


async def _first_attempts_only(setup: Setup) -> None:
    """Every grading but the first attempt gone: a regrade not yet made."""
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(delete(Grading).where(Grading.attempt > 1))


async def test_a_change_to_show_alone_shows_a_grading_with_the_latest_groups(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _three_groups(setup, acme, entered.task)
    await _submit(setup, acme, entered)
    await _graded(setup, RESULT)
    current = await _task_yaml(acme, entered.task)
    shown_now = current.replace(b"small: {pass: 30, show: verdict}", b"small: {pass: 30}")
    await _save(setup, acme, entered.task, {"task.yaml": shown_now})
    await _first_attempts_only(setup)

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    assert submission.grading is not None
    small = submission.grading.groups[1]
    assert (small.show, small.tests) == (Show.ALWAYS, (_credited(2),))


async def test_a_grading_whose_tests_changed_since_is_shown_with_its_own_publication(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _three_groups(setup, acme, entered.task)
    await _submit(setup, acme, entered)
    await _graded(setup, RESULT)
    current = await _task_yaml(acme, entered.task)
    start = current.index(b"test_groups:")
    await _save(
        setup,
        acme,
        entered.task,
        {
            "task.yaml": current[:start]
            + b"test_groups:\n  main: {each: 100}\n  small: {pass: 30}\n"
            + b"  large: {pass: 70, show: after_close}\n  extra: {each: 10, show: always}\n",
            "tests/extra/1/input": b"2 2\n",
            "tests/extra/1/answer": b"4\n",
        },
    )
    await _first_attempts_only(setup)

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    assert submission.grading is not None
    assert submission.grading.outcome == "accepted"
    assert [(group.group, group.show, group.tests) for group in submission.grading.groups] == [
        ("main", Show.ALWAYS, (_credited(1),)),
        ("small", Show.VERDICT, None),
        ("large", Show.AFTER_CLOSE, None),
    ]


async def test_an_extension_holds_the_reveal_back_for_everyone(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _three_groups(setup, acme, entered.task)
    await _submit(setup, acme, entered)
    await _graded(setup, RESULT)
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(hours=1), tasks=["sum"])

    clock.set(END)
    await identity.current(entered.session.id, setup=setup)
    [held] = await submissions.mine(setup, entered.session, entered.task)

    assert held.grading is not None
    assert held.grading.groups[2] == GroupShown(
        group="large",
        show=Show.AFTER_CLOSE,
        outcome=None,
        tests=None,
        shown_at=END + timedelta(hours=1),
        max=Fraction(35),
    )


async def test_a_stop_is_shown_as_the_outcome(setup: Setup, acme: Acme, entered: Entered) -> None:
    await _submit(setup, acme, entered)
    stopped = {
        **RESULT,
        "stopped": "compile_error",
        "stopped_by": "compile",
        "tests": [{"test": "main/1", "outcome": "skipped", "values": {}}],
        "values": {"log": "main.py:1: SyntaxError"},
    }
    await _graded(setup, stopped)

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    assert submission.grading is not None
    assert (submission.grading.stopped, submission.grading.outcome) == (
        "compile_error",
        "compile_error",
    )
    assert submission.grading.values == {"log": "main.py:1: SyntaxError"}


async def test_a_system_error_is_read_by_its_contestant_as_still_running(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    broken = {
        **RESULT,
        "stopped": "system_error",
        "values": {},
        "error": "The step check wrote no outcome.",
    }
    await _graded(setup, broken, status="system_error")

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    assert submission.grading is not None
    assert submission.grading.status == GradingStatus.RUNNING
    assert (
        submission.grading.stopped,
        submission.grading.outcome,
        submission.grading.groups,
        submission.grading.values,
    ) == (None, None, (), {})


async def test_a_submission_after_the_rows_due_is_late_by_its_started_days(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await write_contest(
        acme.fake,
        RUNNING.format(visibility="everyone")
        + "    due: 2026-09-26T11:00:00Z\n    late_per_day: 0.5\n",
    )
    first = await _submit(setup, acme, entered)
    assert first.late_days == 1  # an hour after the due is a started day

    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(hours=2), tasks=["sum"])
    clock.advance(timedelta(minutes=30))
    second = await _submit(setup, acme, entered, key="key-0002-bbbb", content=b"print(2)\n")
    clock.advance(timedelta(hours=1))
    third = await _submit(setup, acme, entered, key="key-0003-cccc", content=b"print(3)\n")

    # The extension moves bob's due to 13:00: the first, at noon, is on time
    # now, and the third, at 13:30, is late.
    assert (second.late_days, third.late_days) == (0, 1)
    listed = await submissions.mine(setup, entered.session, entered.task)
    assert [(made.number, made.late_days) for made in listed] == [(3, 1), (2, 0), (1, 0)]


async def test_a_place_to_submit_a_try_left_half_made_is_finished_by_the_next_submit(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.state.create_repo(
        PLATFORM, "acme", "spring.sum.u8.sub", {}, scope=Scope("acme", "spring")
    )

    submission = await _submit(setup, acme, entered)

    assert submission.number == 1
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    assert repo.writers == {8}
