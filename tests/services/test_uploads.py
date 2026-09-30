"""A contestant's files on their way in. A slot leaves one `uploads` row and a
form the store takes the file through until it expires; one larger than the
task's or the input's limit is refused before any row or URL, naming the
limit. Completing measures what arrived: the size declared is `verified`
with its digest, any other `rejected`. A file over 16MB goes in parts of
exact lengths and becomes usable only once they are joined. Nobody reaches
another person's upload, and a slot is only for someone who may submit now.
One person holds a bounded number of unsubmitted uploads for a task, and a
bounded number of bytes declared by them, a rejected one included, even
when two slots are asked at once. A failing store is told to the caller in
fixed words, never its own. The hourly sweep removes, after two days, every upload no submit used,
object and row.
"""

import asyncio
import hashlib
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from forge.db.tables import Upload as UploadRow
from forge.domain import uploads as rules
from forge.domain.content import Edit
from forge.domain.errors import (
    Forbidden,
    InvalidInputs,
    Misconfigured,
    NotApproved,
    NotFound,
    Rejected,
    TaskClosed,
    TooLarge,
    Unavailable,
    UploadLimit,
    UploadNotReady,
)
from forge.domain.identity import PLATFORM
from forge.domain.uploads import PART_SIZE, SINGLE_REQUEST_MAX, UploadStatus
from forge.domain.workflows import Visibility
from forge.port.objects import FinishedPart, Store
from forge.runtime.setup import Setup
from forge.services import publications, uploads
from forge.testing import CLASSIC, FakeClock
from tests.services.conftest import Acme, Entered, signed_in, upload

SOURCE = b"print(1)\n"


async def _rows(setup: Setup) -> list[UploadRow]:
    async with setup.unit_of_work() as ctx:
        return list((await ctx.db.execute(select(UploadRow))).scalars())


async def test_a_slot_leaves_one_row_and_a_form_that_takes_the_file_until_it_expires(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    slot = await uploads.slot(
        setup, entered.session, entered.task, input="submission", filename="main.py", size=5
    )

    assert isinstance(slot, uploads.PostSlot)
    assert slot.expires_at == clock.now() + timedelta(minutes=15)
    assert slot.fields["key"] == f"uploads/{slot.id}"
    [row] = await _rows(setup)
    assert (row.id, row.owner_user_id, row.purpose, row.task_id, row.input_id) == (
        slot.id,
        8,
        "submission",
        entered.task,
        "submission",
    )
    assert (row.declared_size, row.status, row.object_key) == (5, "presigned", slot.fields["key"])
    assert row.expires_at == clock.now() + timedelta(days=2)
    with pytest.raises(Rejected):
        acme.fake.objects.post(slot.fields, b"six b.")
    acme.fake.objects.post(slot.fields, b"12345")
    clock.advance(timedelta(minutes=15))
    with pytest.raises(Forbidden):
        acme.fake.objects.post(slot.fields, b"1234")


async def test_a_slot_over_the_tasks_limit_is_refused_naming_it_before_anything(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    limit = 10 * 1024 * 1024

    with pytest.raises(TooLarge) as refused:
        await uploads.slot(
            setup,
            entered.session,
            entered.task,
            input="submission",
            filename="main.py",
            size=limit + 1,
        )

    assert (refused.value.extra["limit"], refused.value.extra["input"]) == (limit, None)
    assert await _rows(setup) == []
    assert acme.fake.objects.objects[Store.UPLOADS] == {}
    inside = await uploads.slot(
        setup, entered.session, entered.task, input="submission", filename="main.py", size=limit
    )
    assert isinstance(inside, uploads.PostSlot)


async def test_a_file_input_checks_its_own_limit_and_what_it_accepts(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    source = CLASSIC.replace(b"name: unicon/classic", b"name: acme/notes").replace(
        b"  - id: testcases\n", b"  - id: notes\n    type: file\n  - id: testcases\n"
    )
    workflow = await acme.fake.workflows.create_workflow(
        PLATFORM, "acme", "notes", {"workflow.yaml": source}, Visibility.PUBLIC
    )
    await acme.fake.workflows.create_workflow_version(PLATFORM, workflow, "v1")
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    text = current.content.replace(b"unicon/classic@v1", b"acme/notes@v1").replace(
        b"  setter:\n",
        b"    - {id: notes, type: file, accept: ['.txt'], max_size: 1KB}\n  setter:\n",
    )
    saved = await publications.save(
        setup, acme.ada, entered.task, {"task.yaml": Edit(text, head.tokens["task.yaml"])}
    )
    assert isinstance(saved, publications.Published), saved
    session, task = entered.session, entered.task

    with pytest.raises(InvalidInputs) as refused:
        await uploads.slot(setup, session, task, input="notes", filename="x.md", size=1)
    assert refused.value.extra["errors"] == [
        {"input": "notes", "message": "This input takes .txt."}
    ]
    with pytest.raises(TooLarge) as large:
        await uploads.slot(setup, session, task, input="notes", filename="x.txt", size=1025)
    assert (large.value.extra["limit"], large.value.extra["input"]) == (1024, "notes")
    fits = await uploads.slot(setup, session, task, input="notes", filename="x.txt", size=1024)
    assert isinstance(fits, uploads.PostSlot)


async def test_a_slot_needs_a_file_input_a_plain_name_and_an_approved_contestant_now(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock
) -> None:
    session, task = entered.session, entered.task
    with pytest.raises(InvalidInputs) as refused:
        await uploads.slot(setup, session, task, input="time_limit", filename="a", size=1)
    assert refused.value.extra["errors"][0]["input"] == "time_limit"
    with pytest.raises(InvalidInputs):
        await uploads.slot(setup, session, task, input="submission", filename="a/b.py", size=1)
    with pytest.raises(InvalidInputs):
        await uploads.slot(setup, session, task, input="submission", filename="a.py", size=-1)

    acme.fake.add_user(30, "cyd")
    stranger = await signed_in(setup, acme.fake, 30)
    with pytest.raises(NotApproved):
        await uploads.slot(setup, stranger, task, input="submission", filename="a.py", size=1)

    clock.advance(timedelta(hours=3))
    with pytest.raises(TaskClosed) as closed:
        await uploads.slot(setup, session, task, input="submission", filename="a.py", size=1)
    assert closed.value.extra["reason"] == "ended"
    assert len(await _rows(setup)) == 0


async def test_completing_measures_what_arrived_and_a_mismatch_is_rejected(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    session, task = entered.session, entered.task
    verified = await upload(setup, acme.fake, session, task, b"print(1)\n")

    assert verified.status is UploadStatus.VERIFIED
    assert (verified.size, verified.sha256) == (9, hashlib.sha256(b"print(1)\n").hexdigest())
    assert await uploads.complete(setup, session, task, verified.id) == verified

    short = await uploads.slot(
        setup, session, task, input="submission", filename="short.py", size=10
    )
    assert isinstance(short, uploads.PostSlot)
    with pytest.raises(UploadNotReady):
        await uploads.complete(setup, session, task, short.id)
    acme.fake.objects.post(short.fields, b"123")
    rejected = await uploads.complete(setup, session, task, short.id)
    assert (rejected.status, rejected.size) == (UploadStatus.REJECTED, 3)


async def test_a_person_holds_a_bounded_number_of_uploads_until_a_submit_uses_them(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules, "OPEN_MAX", 3)
    session, task = entered.session, entered.task
    await upload(setup, acme.fake, session, task, SOURCE)
    short = await uploads.slot(setup, session, task, input="submission", filename="a.py", size=9)
    assert isinstance(short, uploads.PostSlot)
    acme.fake.objects.post(short.fields, b"1")
    assert (await uploads.complete(setup, session, task, short.id)).status is (
        UploadStatus.REJECTED
    )
    await uploads.slot(setup, session, task, input="submission", filename="b.py", size=1)

    with pytest.raises(UploadLimit) as refused:
        await uploads.slot(setup, session, task, input="submission", filename="c.py", size=1)

    assert refused.value.extra == {"limit": 3, "bytes": 2 * 10 * 1024 * 1024}
    assert len(await _rows(setup)) == 3


async def test_a_person_declares_at_most_two_submissions_worth_of_open_uploads(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    session, task = entered.session, entered.task
    limit = 10 * 1024 * 1024
    for name in ("a.py", "b.py"):
        await uploads.slot(setup, session, task, input="submission", filename=name, size=limit)

    with pytest.raises(UploadLimit) as refused:
        await uploads.slot(setup, session, task, input="submission", filename="c.py", size=1)

    assert refused.value.extra["bytes"] == 2 * limit
    empty = await uploads.slot(setup, session, task, input="submission", filename="d.py", size=0)
    assert isinstance(empty, uploads.PostSlot)


async def test_two_slots_asked_at_once_for_the_last_place_leave_one_row(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _big_task(setup, acme, entered)
    monkeypatch.setattr(rules, "OPEN_MAX", 1)
    session, task = entered.session, entered.task
    start_parts = acme.fake.objects.start_parts

    async def slow(key: str) -> str:
        await asyncio.sleep(0.2)
        return await start_parts(key)

    monkeypatch.setattr(acme.fake.objects, "start_parts", slow)
    size = SINGLE_REQUEST_MAX + 1

    outcomes = await asyncio.gather(
        uploads.slot(setup, session, task, input="submission", filename="a.py", size=size),
        uploads.slot(setup, session, task, input="submission", filename="b.py", size=size),
        return_exceptions=True,
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == ["PartsSlot", "UploadLimit"]
    assert len(await _rows(setup)) == 1


async def test_a_failing_store_is_told_in_fixed_words_and_the_upload_can_be_completed_later(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _big_task(setup, acme, entered)
    session, task = entered.session, entered.task
    store = acme.fake.objects

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise Rejected("the store refused the request (MalformedXML)")

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the store answered 503")

    async def wrong_key(*args: Any, **kwargs: Any) -> Any:
        raise Misconfigured("the store refused the platform's key (InvalidAccessKeyId)")

    with monkeypatch.context() as patched:
        patched.setattr(store, "start_parts", refused)
        with pytest.raises(Unavailable) as failed:
            await uploads.slot(
                setup,
                session,
                task,
                input="submission",
                filename="big.py",
                size=SINGLE_REQUEST_MAX + 1,
            )
    assert failed.value.detail == uploads.STORE_UNAVAILABLE
    assert await _rows(setup) == []

    slot = await uploads.slot(
        setup, session, task, input="submission", filename="main.py", size=len(SOURCE)
    )
    assert isinstance(slot, uploads.PostSlot)
    store.post(slot.fields, SOURCE)
    with monkeypatch.context() as patched:
        patched.setattr(store, "measure", down)
        with pytest.raises(Unavailable) as failed:
            await uploads.complete(setup, session, task, slot.id)
    assert failed.value.detail == uploads.STORE_UNAVAILABLE
    [row] = await _rows(setup)
    assert row.status == "presigned"
    assert (await uploads.complete(setup, session, task, slot.id)).status is UploadStatus.VERIFIED

    parts = await uploads.slot(
        setup, session, task, input="submission", filename="big.py", size=SINGLE_REQUEST_MAX + 1
    )
    assert isinstance(parts, uploads.PartsSlot)
    finished = [FinishedPart(part.number, "etag") for part in parts.parts]
    with monkeypatch.context() as patched:
        patched.setattr(store, "finish_parts", wrong_key)
        with pytest.raises(Misconfigured) as misconfigured:
            await uploads.complete(setup, session, task, parts.id, parts=finished)
    assert misconfigured.value.detail == uploads.STORE_MISCONFIGURED
    with pytest.raises(UploadNotReady) as unready:
        await uploads.complete(setup, session, task, parts.id, parts=finished)
    assert unready.value.detail == "The parts named are not the ones that arrived."


async def test_nobody_completes_another_persons_upload(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    mine = await upload(setup, acme.fake, entered.session, entered.task, b"x")
    acme.fake.add_user(30, "cyd")
    other = await signed_in(setup, acme.fake, 30)

    with pytest.raises(NotFound, match="no such upload"):
        await uploads.complete(setup, other, entered.task, mine.id)


async def _big_task(setup: Setup, acme: Acme, entered: Entered) -> None:
    head = await acme.fake.content.list_files(PLATFORM, entered.task)
    current = await acme.fake.content.read_file(PLATFORM, entered.task, "task.yaml")
    text = current.content.replace(b"max_size: 10MB", b"max_size: 64MB")
    result = await publications.save(
        setup,
        acme.ada,
        entered.task,
        {"task.yaml": Edit(text, head.tokens["task.yaml"])},
        confirm=True,
    )
    assert isinstance(result, publications.Published), result


async def test_a_large_file_goes_in_parts_of_exact_length_and_is_measured_once_joined(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await _big_task(setup, acme, entered)
    content = b"a" * (SINGLE_REQUEST_MAX + 7)
    session, task = entered.session, entered.task
    slot = await uploads.slot(
        setup, session, task, input="submission", filename="big.py", size=len(content)
    )

    assert isinstance(slot, uploads.PartsSlot)
    assert slot.part_size == PART_SIZE
    assert [part.number for part in slot.parts] == list(
        range(1, SINGLE_REQUEST_MAX // PART_SIZE + 2)
    )
    with pytest.raises(Forbidden):
        acme.fake.objects.put_part(slot.parts[-1].url, b"12345678")
    etags = [
        acme.fake.objects.put_part(part.url, content[index * PART_SIZE : (index + 1) * PART_SIZE])
        for index, part in enumerate(slot.parts)
    ]
    finished = [
        FinishedPart(part.number, etag) for part, etag in zip(slot.parts, etags, strict=True)
    ]
    with pytest.raises(UploadNotReady):
        await uploads.complete(setup, session, task, slot.id, parts=finished[:-1])
    [row] = await _rows(setup)
    assert (row.status, row.multipart_upload_id is not None) == ("presigned", True)

    done = await uploads.complete(setup, session, task, slot.id, parts=finished)

    assert (done.status, done.size) == (UploadStatus.VERIFIED, len(content))
    assert done.sha256 == hashlib.sha256(content).hexdigest()
    [row] = await _rows(setup)
    assert row.multipart_upload_id is None


async def test_the_sweep_takes_every_due_upload_however_many_there_are(
    setup: Setup, acme: Acme, entered: Entered, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(3):
        await upload(
            setup,
            acme.fake,
            entered.session,
            entered.task,
            f"print({number})\n".encode(),
            filename=f"{number}.py",
        )
    monkeypatch.setattr(uploads, "SWEEP_BATCH", 2)
    clock.advance(timedelta(days=2))

    async with setup.unit_of_work() as ctx:
        assert await uploads.sweep(ctx) == 3

    assert await _rows(setup) == []
    assert acme.fake.objects.objects[Store.UPLOADS] == {}
