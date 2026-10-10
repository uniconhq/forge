"""A person's files on their way in, through the upload door.

A slot leaves one `uploads` row and an address on the platform's own host;
one larger than its input's `max_size` is refused before any row, naming the
limit and the input, and one for an input that takes no file, or at a path
the input does not take, is refused too. The door is what the proxy asks before it reads a body: it
opens only for the owner's own waiting upload of exactly the length the
request carries, and what it answers with is the person's own credential, so
the forge's check of their write access stays underneath. Completing asks
the forge whether the place holds the object; a file the forge already holds
needs no upload at all.

Nobody reaches another person's upload, and a slot is only for someone who
may submit now. One person holds a bounded number of unsubmitted uploads for
a task, and a bounded number of bytes declared by them, even when two slots
are asked at once. A failing forge is told to the caller in fixed words,
never its own. A slot removes its owner's rows that lapsed two days ago, and
a submit marks the ones it took, whose bytes belong to the commit from then
on.
"""

import asyncio
import hashlib
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from forge.adapters.ids import parse_task, parse_workspace
from forge.db.tables import Upload as UploadRow
from forge.domain import uploads as rules
from forge.domain.content import Edit
from forge.domain.errors import (
    Forbidden,
    InvalidInputs,
    NotApproved,
    NotFound,
    TaskClosed,
    TooLarge,
    Unavailable,
    UploadLimit,
    UploadNotReady,
)
from forge.domain.identity import PLATFORM
from forge.domain.ids import ContestId
from forge.domain.names import UserOwner
from forge.domain.roles import Role, Scope
from forge.domain.submissions import SubmittedInput
from forge.domain.uploads import UploadStatus
from forge.domain.workflows import Visibility
from forge.port.uploads import SubmissionPlace
from forge.runtime.setup import Setup
from forge.services import contestants, publications, submissions, submitters, uploads
from forge.testing import FakeClock
from tests.services.conftest import SPRING, Acme, Entered, organiser, signed_in, upload

SOURCE = b"print(1)\n"
INPUT_LIMIT = 10 * 1024 * 1024


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _rows(setup: Setup) -> list[UploadRow]:
    async with setup.unit_of_work() as ctx:
        return list((await ctx.db.execute(select(UploadRow))).scalars())


def _place(acme: Acme, entered: Entered) -> SubmissionPlace:
    workspace = acme.fake.workspaces.workspace_of(ContestId("acme/spring"), UserOwner(8))
    return SubmissionPlace(workspace, entered.task)


async def test_a_slot_leaves_one_row_and_an_address_on_the_platforms_own_host(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    slot = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=5,
        sha256=_digest(b"12345"),
    )

    assert slot.url == f"/-/uploads/{slot.id}"
    assert not slot.ready
    assert slot.expires_at == clock.now() + timedelta(days=2)
    [row] = await _rows(setup)
    assert (row.id, row.owner_user_id, row.purpose, row.task_id, row.input_id) == (
        slot.id,
        8,
        "submission",
        entered.task,
        "submission",
    )
    assert (row.size, row.status, row.digest) == (5, "waiting", _digest(b"12345"))
    assert row.repo_path is None


async def test_the_place_to_submit_is_made_before_any_bytes_are_asked_for(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # An object belongs to a repository at the forge, so there has to be one
    # before the browser sends anything.
    assert acme.fake.calls_to("open_submission_place") == []

    await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )

    assert len(acme.fake.calls_to("open_submission_place")) == 1


async def test_the_door_opens_once_for_the_owner_and_carries_their_own_credential(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )

    door = await uploads.door(setup, entered.session, slot.id, length=len(SOURCE))

    assert _digest(SOURCE) in door.path and str(len(SOURCE)) in door.path
    assert door.authorization.startswith("Basic ")
    # Sending it again after a dropped line asks the same door again.
    again = await uploads.door(setup, entered.session, slot.id, length=len(SOURCE))
    assert again == door
    acme.fake.uploads.send(door.path, door.authorization, SOURCE)
    done = await uploads.complete(setup, entered.session, entered.task, slot.id)
    assert done.status is UploadStatus.VERIFIED
    # Once it has arrived the door will not open for it again.
    with pytest.raises(Forbidden):
        await uploads.door(setup, entered.session, slot.id, length=len(SOURCE))


async def test_the_door_refuses_a_stranger_a_wrong_length_and_an_unknown_upload(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )
    acme.fake.add_user(30, "cyd")
    stranger = await signed_in(setup, acme.fake, 30)

    # Each refusal says the same thing, so nothing is learned from which.
    for refusal in (
        uploads.door(setup, stranger, slot.id, length=len(SOURCE)),
        uploads.door(setup, entered.session, slot.id, length=len(SOURCE) + 1),
        uploads.door(setup, entered.session, uuid.uuid4(), length=len(SOURCE)),
    ):
        with pytest.raises(Forbidden) as refused:
            await refusal
        assert refused.value.detail == uploads.DOOR_REFUSED


async def test_a_file_the_forge_already_holds_needs_no_upload(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    first = await upload(setup, acme.fake, entered.session, entered.task, SOURCE)
    assert first.status is UploadStatus.VERIFIED

    again = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )

    assert again.ready and again.url is None
    assert (
        await uploads.complete(setup, entered.session, entered.task, again.id)
    ).status is UploadStatus.VERIFIED


async def test_completing_before_the_bytes_arrive_says_so_and_can_be_asked_again(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    session, task = entered.session, entered.task
    slot = await uploads.slot(
        setup,
        session,
        task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )

    with pytest.raises(UploadNotReady):
        await uploads.complete(setup, session, task, slot.id)

    door = await uploads.door(setup, session, slot.id, length=len(SOURCE))
    acme.fake.uploads.send(door.path, door.authorization, SOURCE)
    done = await uploads.complete(setup, session, task, slot.id)
    assert (done.status, done.size, done.sha256) == (
        UploadStatus.VERIFIED,
        len(SOURCE),
        _digest(SOURCE),
    )
    assert await uploads.complete(setup, session, task, slot.id) == done


async def test_a_slot_over_its_inputs_default_size_is_refused_naming_it_before_anything(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    with pytest.raises(TooLarge) as refused:
        await uploads.slot(
            setup,
            entered.session,
            entered.task,
            input="submission",
            filename="main.py",
            size=INPUT_LIMIT + 1,
            sha256=_digest(b"x"),
        )

    assert (refused.value.extra["limit"], refused.value.extra["input"]) == (
        INPUT_LIMIT,
        "submission",
    )
    assert await _rows(setup) == []
    inside = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=INPUT_LIMIT,
        sha256=_digest(b"x"),
    )
    assert inside.url is not None


async def test_a_file_input_checks_the_size_its_task_gives_it(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    text = current.content.replace(
        b"submission: {label: Your solution}", b"submission: {label: Your solution, max_size: 1KB}"
    )
    assert text != current.content
    saved = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(text, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved
    session, task = entered.session, entered.task
    digest = _digest(b"x")

    with pytest.raises(TooLarge) as large:
        await uploads.slot(
            setup, session, task, input="submission", filename="x.py", size=1025, sha256=digest
        )
    assert (large.value.extra["limit"], large.value.extra["input"]) == (1024, "submission")
    fits = await uploads.slot(
        setup, session, task, input="submission", filename="x.py", size=1024, sha256=digest
    )
    assert fits.url is not None


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


async def test_a_per_test_slot_is_named_for_a_test_with_an_ending_that_is_not_empty(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    made = await acme.fake.workflows.create_workflow(
        PLATFORM, "acme", "answers", {"workflow.yaml": ANSWERS}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, made, "v1")
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    text = b"name: Sum\nworkflow: acme/answers@v1\ntest_groups:\n  main: {each: 100}\n"
    saved = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(text, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(saved, publications.Published), saved
    session, task = entered.session, entered.task
    digest = _digest(b"3\n")

    with pytest.raises(InvalidInputs) as refused:
        await uploads.slot(
            setup, session, task, input="answers", filename="main/1.", size=2, sha256=digest
        )
    assert refused.value.extra["errors"][0]["input"] == "answers"
    for fine in ("main/1", "main/1.t"):
        slot = await uploads.slot(
            setup, session, task, input="answers", filename=fine, size=2, sha256=digest
        )
        assert slot.url is not None


async def test_a_slot_needs_a_file_input_a_plain_name_a_digest_and_a_contestant_now(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    session, task = entered.session, entered.task
    digest = _digest(b"x")

    with pytest.raises(InvalidInputs) as refused:
        await uploads.slot(
            setup, session, task, input="time_limit", filename="a", size=1, sha256=digest
        )
    assert refused.value.extra["errors"][0]["input"] == "time_limit"
    with pytest.raises(InvalidInputs) as valued:
        await uploads.slot(
            setup, session, task, input="language", filename="a", size=1, sha256=digest
        )
    assert valued.value.extra["errors"][0]["input"] == "language"
    with pytest.raises(InvalidInputs):
        await uploads.slot(
            setup, session, task, input="submission", filename="a/b.py", size=1, sha256=digest
        )
    with pytest.raises(InvalidInputs):
        await uploads.slot(
            setup, session, task, input="submission", filename="a.py", size=-1, sha256=digest
        )
    with pytest.raises(InvalidInputs):
        await uploads.slot(
            setup, session, task, input="submission", filename="a.py", size=1, sha256="nope"
        )
    with pytest.raises(InvalidInputs):
        await uploads.slot(
            setup, session, task, input="submission", filename="a.py", size=1, sha256=digest.upper()
        )

    acme.fake.add_user(30, "cyd")
    stranger = await signed_in(setup, acme.fake, 30)
    with pytest.raises(NotApproved):
        await uploads.slot(
            setup, stranger, task, input="submission", filename="a.py", size=1, sha256=digest
        )

    clock.advance(timedelta(hours=3))
    with pytest.raises(TaskClosed) as closed:
        await uploads.slot(
            setup, session, task, input="submission", filename="a.py", size=1, sha256=digest
        )
    assert closed.value.extra["reason"] == "closed"
    assert await _rows(setup) == []


async def test_a_person_holds_a_bounded_number_of_uploads_until_a_submit_uses_them(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules, "OPEN_MAX", 3)
    session, task = entered.session, entered.task
    verified = await upload(setup, acme.fake, session, task, SOURCE)
    for name, content in (("a.py", b"print(2)\n"), ("b.py", b"print(3)\n")):
        await uploads.slot(
            setup,
            session,
            task,
            input="submission",
            filename=name,
            size=len(content),
            sha256=_digest(content),
        )

    with pytest.raises(UploadLimit) as refused:
        await uploads.slot(
            setup, session, task, input="submission", filename="c.py", size=1, sha256=_digest(b"c")
        )

    assert refused.value.extra == {"limit": 3, "bytes": 2 * INPUT_LIMIT}
    assert len(await _rows(setup)) == 3
    await submissions.submit(
        setup,
        session,
        task,
        {
            "submission": SubmittedInput(uploads=(verified.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-frees-one",
    )
    freed = await uploads.slot(
        setup, session, task, input="submission", filename="c.py", size=1, sha256=_digest(b"c")
    )
    assert freed.id is not None


async def test_a_person_declares_at_most_two_submissions_worth_of_open_uploads(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    session, task = entered.session, entered.task
    for name in ("a.py", "b.py"):
        await uploads.slot(
            setup,
            session,
            task,
            input="submission",
            filename=name,
            size=INPUT_LIMIT,
            sha256=_digest(name.encode()),
        )

    with pytest.raises(UploadLimit) as refused:
        await uploads.slot(
            setup, session, task, input="submission", filename="c.py", size=1, sha256=_digest(b"c")
        )

    assert refused.value.extra["bytes"] == 2 * INPUT_LIMIT
    empty = await uploads.slot(
        setup, session, task, input="submission", filename="d.py", size=0, sha256=_digest(b"")
    )
    assert empty.id is not None


async def test_two_slots_asked_at_once_for_the_last_place_leave_one_row(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules, "OPEN_MAX", 1)
    session, task = entered.session, entered.task
    holds = acme.fake.uploads.holds

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return await holds(*args, **kwargs)

    monkeypatch.setattr(acme.fake.uploads, "holds", slow)

    outcomes = await asyncio.gather(
        uploads.slot(
            setup, session, task, input="submission", filename="a.py", size=9, sha256=_digest(b"a")
        ),
        uploads.slot(
            setup, session, task, input="submission", filename="b.py", size=9, sha256=_digest(b"b")
        ),
        return_exceptions=True,
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["Slot", "UploadLimit"]
    assert len(await _rows(setup)) == 1


async def test_a_failing_forge_is_told_in_fixed_words_and_the_upload_survives_it(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, task = entered.session, entered.task

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge answered 503")

    with monkeypatch.context() as patched:
        patched.setattr(acme.fake.uploads, "holds", down)
        with pytest.raises(Unavailable) as failed:
            await uploads.slot(
                setup,
                session,
                task,
                input="submission",
                filename="main.py",
                size=len(SOURCE),
                sha256=_digest(SOURCE),
            )
    assert failed.value.detail == uploads.FORGE_UNAVAILABLE
    assert await _rows(setup) == []

    slot = await uploads.slot(
        setup,
        session,
        task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )
    door = await uploads.door(setup, session, slot.id, length=len(SOURCE))
    acme.fake.uploads.send(door.path, door.authorization, SOURCE)
    with monkeypatch.context() as patched:
        patched.setattr(acme.fake.uploads, "holds", down)
        with pytest.raises(Unavailable) as failed:
            await uploads.complete(setup, session, task, slot.id)
    assert failed.value.detail == uploads.FORGE_UNAVAILABLE
    [row] = await _rows(setup)
    assert row.status == "waiting"
    assert (await uploads.complete(setup, session, task, slot.id)).status is UploadStatus.VERIFIED


async def test_nobody_reaches_another_persons_upload(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    mine = await upload(setup, acme.fake, entered.session, entered.task, b"x")
    acme.fake.add_user(30, "cyd")
    other = await signed_in(setup, acme.fake, 30)

    with pytest.raises(NotFound, match="no such upload"):
        await uploads.complete(setup, other, entered.task, mine.id)


async def test_a_submit_keeps_the_row_and_a_slot_clears_its_owners_lapsed_ones(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    session, task = entered.session, entered.task
    used = await upload(setup, acme.fake, session, task, b"print(1)\n")
    unused = await upload(setup, acme.fake, session, task, b"print(2)\n", filename="b.py")
    await submissions.submit(
        setup,
        session,
        task,
        {
            "submission": SubmittedInput(uploads=(used.id,)),
            "language": SubmittedInput(value="python"),
        },
        idempotency_key="key-for-sweep",
    )
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(days=3))

    clock.advance(timedelta(days=1))
    await upload(setup, acme.fake, session, task, b"print(3)\n", filename="c.py")
    assert len(await _rows(setup)) == 3

    clock.advance(timedelta(days=1, seconds=1))
    fresh = await upload(setup, acme.fake, session, task, b"print(4)\n", filename="d.py")

    rows = {row.id: row for row in await _rows(setup)}
    assert unused.id not in rows
    assert set(rows) >= {used.id, fresh.id}
    assert (rows[used.id].status, rows[used.id].consumed_by) == (
        "consumed",
        "acme/spring/@u8/sum#1",
    )
    # The bytes of a submitted file belong to the commit, so the platform
    # asks the forge for nothing to be removed; the forge collects what no
    # commit points at.
    assert await acme.fake.uploads.holds(
        _place(acme, entered),
        as_=PLATFORM,
        digest=_digest(b"print(1)\n"),
        size=len(b"print(1)\n"),
    )


async def test_a_file_the_forge_holds_at_another_length_is_not_ready(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # The forge takes a second send of an object it already holds at whatever
    # length the address claims, and answers its cheaper checks the same way,
    # so the platform measures instead. Without that a person who had
    # uploaded one large file could claim it again at any size, and every
    # pointer written for it would name a length that was not the file's.
    big = b"a model's weights, pretend\n" * 64
    await upload(setup, acme.fake, entered.session, entered.task, big, filename="model.bin")

    lying = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="tiny.bin",
        size=1,
        sha256=_digest(big),
    )

    assert not lying.ready and lying.url is not None
    # And it cannot be completed either, however often it is asked.
    with pytest.raises(UploadNotReady):
        await uploads.complete(setup, entered.session, entered.task, lying.id)
    # Sending the bytes through the door does not make the claim true: the
    # forge links what it already has at the length the address names, and
    # the platform still measures.
    door = await uploads.door(setup, entered.session, lying.id, length=1)
    acme.fake.uploads.send(door.path, door.authorization, b"x")
    with pytest.raises(UploadNotReady):
        await uploads.complete(setup, entered.session, entered.task, lying.id)


async def test_the_door_refuses_an_upload_whose_lifetime_is_over(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    # A row lives two days and is cleared at its owner's next slot, so one can
    # outlive its lifetime; the door is where that stops.
    slot = await uploads.slot(
        setup,
        entered.session,
        entered.task,
        input="submission",
        filename="main.py",
        size=len(SOURCE),
        sha256=_digest(SOURCE),
    )
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    await contestants.extend(setup, manager, SPRING, 8, timedelta(days=5))

    clock.advance(timedelta(days=2, seconds=1))

    with pytest.raises(Forbidden):
        await uploads.door(setup, entered.session, slot.id, length=len(SOURCE))


async def test_a_refusal_at_the_forge_is_not_told_as_a_failure(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A person whose access to their own place has gone is refused, not kept
    # retrying something that can never come right.
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise Forbidden("the forge says no")

    monkeypatch.setattr(acme.fake.uploads, "holds", refused)

    with pytest.raises(Forbidden) as told:
        await uploads.slot(
            setup,
            entered.session,
            entered.task,
            input="submission",
            filename="main.py",
            size=len(SOURCE),
            sha256=_digest(SOURCE),
        )

    assert told.value.detail == uploads.FORGE_REFUSED


async def test_a_removal_while_the_place_is_made_takes_the_access_back(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await organiser(setup, acme.fake, 7, Scope("acme", "spring"), Role.MANAGER)
    real = acme.fake.workspaces.open_submission_place

    async def made_then_removed(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        await contestants.remove(setup, manager, SPRING, 8)

    monkeypatch.setattr(acme.fake.workspaces, "open_submission_place", made_then_removed)

    with pytest.raises(NotApproved):
        await uploads.slot(
            setup,
            entered.session,
            entered.task,
            input="submission",
            filename="main.py",
            size=len(SOURCE),
            sha256=_digest(SOURCE),
        )

    assert await _rows(setup) == []
    made = [
        call.operation
        for call in acme.fake.state.calls
        if call.operation in ("open_submission_place", "close_workspace")
    ]
    assert made == ["open_submission_place", "close_workspace", "close_workspace"]
    ref = parse_workspace(_place(acme, entered).workspace)
    repo = acme.fake.state.repo(ref.org, ref.submission_repo(parse_task(entered.task).task))
    assert 8 not in repo.writers


async def test_places_are_made_a_few_at_a_time_in_turn() -> None:
    making = most = 0
    finished: list[int] = []

    async def make(index: int) -> None:
        nonlocal making, most
        async with submitters._room():
            making += 1
            most = max(most, making)
            await asyncio.sleep(0.01)
            making -= 1
        finished.append(index)

    await asyncio.gather(*(make(index) for index in range(10)))

    assert most == submitters.PLACES_AT_ONCE
    assert finished == list(range(10))
