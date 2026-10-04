"""A contestant's uploads become a submission. A passing submit is one commit
by the contestant with the files under `files/` and `submission.json`, named
`submission/<n>` by the platform, and one queued grading row per stage graded
on submit against the current publication, holding the hash of its callback
token. Each refusal comes before anything is written, with its own code, in
the order the rules give. A number another took first is taken by the next.
The same key sent twice returns the first submission and makes nothing, and
a submit whose forge writes landed but whose rows did not is completed by the
retry instead of made again. The contestant reads their own submissions back,
each grading as its stage shows it, and nobody else's.
"""

import hashlib
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

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
from forge.domain.ids import ContestId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.uploads import pointer_text
from forge.port.uploads import SubmissionPlace
from forge.runtime.setup import Setup
from forge.services import contestants, publications, submissions, uploads
from forge.testing import FakeClock
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


def code(*uploads: Any, language: str = "python") -> dict[str, SubmittedInput]:
    return {"submission": SubmittedInput(uploads=tuple(uploads), language=language)}


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
    [result] = submission.gradings
    assert (result.stage, result.attempt, result.status, result.show) == (
        "default",
        1,
        GradingStatus.QUEUED,
        Show.FULL,
    )
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    head = repo.history[-1]
    assert head.author_id == 8
    assert repo.versions == {"submission/1": head.version}
    assert sorted(repo.snapshots[head.version]) == ["files/submission/main.py", "submission.json"]
    assert repo.snapshots[head.version]["files/submission/main.py"] == pointer_text(
        hashlib.sha256(SOURCE).hexdigest(), len(SOURCE)
    )
    assert json.loads(repo.snapshots[head.version]["submission.json"]) == {
        "schema_version": 4,
        "inputs": {"submission": {"files": ["files/submission/main.py"], "language": "python"}},
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
    assert (row.stage, row.attempt, row.status, row.idempotency_key) == (
        "default",
        1,
        "dispatched",
        KEY,
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
    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    assert sorted(repo.snapshots[repo.versions["submission/2"]]) == [
        "files/submission/b.py",
        "submission.json",
    ]
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
    assert [result.id for result in again.gradings] == [result.id for result in first.gradings]
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


async def test_only_the_stages_graded_on_submit_get_a_row(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    staged = current.content + (
        b"stages:\n  - id: public\n    show: metrics\n  - id: final\n    trigger: at_end\n"
    )
    saved = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(staged, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved

    submission = await _submit(setup, acme, entered)

    assert [(result.stage, result.show) for result in submission.gradings] == [
        ("public", Show.METRICS)
    ]
    [row] = await _gradings(setup)
    assert (row.stage, row.publication_id) == ("public", saved.publication)


async def test_a_submit_needs_a_valid_idempotency_key(setup: Setup, entered: Entered) -> None:
    with pytest.raises(InvalidIdempotencyKey):
        await submissions.submit(
            setup, entered.session, entered.task, code(), idempotency_key="short"
        )


async def test_a_closed_or_archived_task_is_refused_first(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    await write_contest(
        acme.fake, RUNNING.format(visibility="public") + "submissions_closed: true\n"
    )
    with pytest.raises(TaskClosed) as closed:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )
    assert closed.value.extra["reason"] == "submissions_closed"

    await write_contest(
        acme.fake, RUNNING.format(visibility="public").replace("published", "archived")
    )
    with pytest.raises(Archived):
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )

    await write_contest(acme.fake, RUNNING.format(visibility="public"))
    clock.advance(timedelta(hours=3))
    with pytest.raises(TaskClosed) as ended:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
        )
    assert ended.value.extra["reason"] == "ended"
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(hours=4))
    passed = await submissions.submit(
        setup, entered.session, entered.task, code(made.id), idempotency_key=KEY
    )
    assert passed.number == 1
    assert acme.fake.calls_to("record_submission") != []


async def test_someone_not_approved_is_refused(setup: Setup, acme: Acme, entered: Entered) -> None:
    acme.fake.add_user(30, "cyd")
    cyd = await signed_in(setup, acme.fake, 30)
    await contestants.register(setup, cyd, SPRING)
    with pytest.raises(NotApproved):
        await submissions.submit(setup, cyd, entered.task, code(), idempotency_key=KEY)
    assert acme.fake.calls_to("record_submission") == []
    assert acme.fake.calls_to("open_submission_place") == []


async def _limits(setup: Setup, acme: Acme, entered: Entered, limits: bytes) -> None:
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    start = current.content.index(b"limits:")
    text = current.content[:start] + b"limits:\n" + limits
    saved = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(text, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved


async def test_the_count_and_rate_limits_hold(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _limits(setup, acme, entered, b"  submissions: 2\n  rate: 1 per 60s\n  max_size: 10MB\n")
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


async def test_a_submission_over_the_tasks_size_is_refused(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _limits(setup, acme, entered, b"  max_size: 1KB\n")
    made = await upload(setup, acme.fake, entered.session, entered.task, b"x" * 1000)
    extra = await upload(
        setup, acme.fake, entered.session, entered.task, b"y" * 100, filename="b.py"
    )

    with pytest.raises(TooLarge) as refused:
        await submissions.submit(
            setup, entered.session, entered.task, code(made.id, extra.id), idempotency_key=KEY
        )
    assert (refused.value.extra["limit"], refused.value.extra["input"]) == (1024, None)


async def test_what_does_not_fit_the_inputs_is_refused_and_nothing_is_written(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)

    with pytest.raises(InvalidInputs) as refused:
        await submissions.submit(
            setup,
            entered.session,
            entered.task,
            code(made.id, language="rust"),
            idempotency_key=KEY,
        )

    assert refused.value.extra["errors"][0]["input"] == "submission"
    assert acme.fake.calls_to("record_submission") == []
    assert await _gradings(setup) == []


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

    repo = acme.fake.state.repos[("acme", "spring.sum.u8.sub")]
    written = repo.snapshots[repo.history[-1].version]
    path = next(name for name in written if name.startswith("files/"))
    assert written[path] == pointer_text(hashlib.sha256(SOURCE).hexdigest(), len(SOURCE))
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


async def test_a_contestant_reads_their_own_submissions_as_each_stage_shows_them(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    await _submit(setup, acme, entered)
    clock.advance(timedelta(minutes=1))
    await _submit(setup, acme, entered, key="key-0002-bbbb", content=b"print(2)\n")
    verdict = {
        "outcome": "accepted",
        "metrics": {"points": 1},
        "tests": [{"id": "1", "outcome": "accepted"}],
        "summary": "1 of 1 tests accepted.",
    }
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading)
            .where(Grading.submission_number == 1)
            .values(status="done", verdict=verdict, log_key="logs/x/1.log")
        )

    listed = await submissions.mine(setup, entered.session, entered.task)

    assert [submission.number for submission in listed] == [2, 1]
    [result] = listed[1].gradings
    assert (result.status, result.outcome, result.metrics, result.summary) == (
        GradingStatus.DONE,
        "accepted",
        {"points": 1},
        "1 of 1 tests accepted.",
    )
    assert (result.tests, result.log) == (({"id": "1", "outcome": "accepted"},), True)
    assert await submissions.one(setup, entered.session, entered.task, 1) == listed[1]
    files = await submissions.files(setup, entered.session, entered.task, 2)
    assert files.inputs == {
        "submission": {"files": ["files/submission/main.py"], "language": "python"}
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


async def test_a_system_error_shows_its_outcome_and_never_its_summary(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    verdict = {
        "outcome": "system_error",
        "metrics": {},
        "tests": [],
        "summary": "The step compile could not start: the filter refused it.",
    }
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(update(Grading).values(status="system_error", verdict=verdict))

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    [result] = submission.gradings
    assert (result.status, result.outcome, result.summary) == (
        GradingStatus.SYSTEM_ERROR,
        "system_error",
        None,
    )


async def test_a_hidden_stage_shows_the_status_alone(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _submit(setup, acme, entered)
    async with setup.unit_of_work() as ctx:
        await ctx.db.execute(
            update(Grading).values(status="done", verdict={"outcome": "accepted"}, log_key="l")
        )
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    hidden = current.content + b"stages:\n  - id: default\n    show: hidden\n"
    saved = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(hidden, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved

    [submission] = await submissions.mine(setup, entered.session, entered.task)

    [result] = submission.gradings
    assert (result.status, result.show, result.outcome, result.log) == (
        GradingStatus.DONE,
        Show.HIDDEN,
        None,
        False,
    )


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
