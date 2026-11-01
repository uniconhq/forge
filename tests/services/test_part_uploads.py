"""Organiser multipart transfers through the object store and verified LFS boundary."""

import hashlib
from datetime import timedelta

import pytest

from forge.db.tables import PartUpload
from forge.domain.errors import InvalidInputs, NotFound, Rejected, TooLarge, UploadLimit
from forge.domain.ids import TaskId
from forge.port.uploads import TaskPlace
from forge.runtime.setup import Setup
from forge.services import part_uploads, uploads
from forge.settings import Settings
from forge.testing import FakeClock
from tests.services.conftest import Acme, signed_in


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(database_url=migrated_database_url, upload_part_size=5 * 1024**2)


async def _start(
    setup: Setup,
    acme: Acme,
    task: TaskId,
    body: bytes,
    *,
    path: str = "data.bin",
    digest: str | None = None,
) -> part_uploads.Transfer:
    return await part_uploads.start_task(
        setup,
        acme.ada,
        task,
        path=path,
        digest=digest or hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


async def _send(setup: Setup, acme: Acme, transfer: part_uploads.Transfer, body: bytes) -> None:
    owner = await signed_in(setup, acme.fake, 7)
    for index, offset in enumerate(range(0, len(body), transfer.part_size), 1):
        url = (await part_uploads.sign(setup, owner, transfer.id, [index]))[index]
        acme.fake.objects.put(url, body[offset : offset + transfer.part_size])


async def test_parts_resume_and_only_verified_forgejo_storage_enters_the_normal_save(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    body = b"x" * (5 * 1024**2) + b"end"
    made = await _start(setup, acme, sum_task, body)
    owner = await signed_in(setup, acme.fake, 7)
    url = (await part_uploads.sign(setup, owner, made.id, [1]))[1]
    acme.fake.objects.put(url, body[: made.part_size])
    resumed = await part_uploads.read(setup, owner, made.id)
    assert [(part.number, part.size) for part in resumed.parts] == [(1, made.part_size)]
    same = await _start(setup, acme, sum_task, body)
    assert same.id == made.id and same.parts == resumed.parts
    async with setup.unit_of_work() as ctx:
        assert await uploads.for_save(ctx, 7, sum_task, [made.id]) == {}
    url = (await part_uploads.sign(setup, owner, made.id, [2]))[2]
    acme.fake.objects.put(url, body[made.part_size :])
    found = await part_uploads.read(setup, owner, made.id)
    finishing = await part_uploads.finish(
        setup, owner, made.id, [(part.number, part.etag) for part in found.parts]
    )
    assert finishing.status == "finishing"
    await setup.settle()
    stored = await part_uploads.read(setup, owner, made.id)
    assert stored.status == "stored" and stored.parts == ()
    assert [row.id for row in await part_uploads.opened(setup, owner, TaskPlace(sum_task))] == [
        made.id
    ]
    assert (await _start(setup, acme, sum_task, body)).id == made.id
    assert acme.fake.objects.uploads == {} and acme.fake.objects.multipart == {}
    assert await acme.fake.uploads.holds(
        TaskPlace(sum_task), as_=acme.ada.identity, digest=made.sha256, size=len(body)
    )
    async with setup.unit_of_work() as ctx:
        row = (await uploads.for_save(ctx, 7, sum_task, [made.id]))[made.id]
        assert row.status == "verified" and row.repo_path == "data.bin"
    completed = await uploads.complete(setup, owner, sum_task, made.id)
    assert completed.status == "verified"


async def test_a_digest_refused_by_forgejo_is_deleted_and_reported(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    body = b"not the declared digest"
    made = await _start(setup, acme, sum_task, body, digest="f" * 64)
    owner = await signed_in(setup, acme.fake, 7)
    await _send(setup, acme, made, body)
    found = await part_uploads.read(setup, owner, made.id)
    await part_uploads.finish(
        setup, owner, made.id, [(part.number, part.etag) for part in found.parts]
    )
    await setup.settle()
    refused = await part_uploads.read(setup, owner, made.id)
    assert refused.status == "refused" and refused.reason == "content hash does not match OID"
    assert acme.fake.objects.uploads == {}
    async with setup.unit_of_work() as ctx:
        assert await uploads.for_save(ctx, 7, sum_task, [made.id]) == {}


async def test_a_failed_temporary_delete_is_reported_without_waiting_for_the_deadline(
    setup: Setup, acme: Acme, sum_task: TaskId, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"stored but cleanup failed"
    made = await _start(setup, acme, sum_task, body)
    owner = await signed_in(setup, acme.fake, 7)
    await _send(setup, acme, made, body)
    found = await part_uploads.read(setup, owner, made.id)
    original = acme.fake.objects.delete_upload
    calls = 0

    async def failing(key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Rejected("Temporary storage was unavailable; send again.")
        await original(key)

    monkeypatch.setattr(acme.fake.objects, "delete_upload", failing)
    await part_uploads.finish(
        setup, owner, made.id, [(part.number, part.etag) for part in found.parts]
    )
    await setup.settle()
    state = await part_uploads.read(setup, owner, made.id)
    assert state.status == "refused" and "unavailable" in (state.reason or "")
    assert not acme.fake.objects.uploads


async def test_an_object_already_held_is_stored_without_parts(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    body = b"already here"
    acme.fake.uploads.put(TaskPlace(sum_task), body)
    made = await _start(setup, acme, sum_task, body)
    assert made.status == "stored" and made.parts == ()
    assert acme.fake.objects.multipart == {}


async def test_only_the_owner_may_read_sign_finish_or_abort(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    made = await _start(setup, acme, sum_task, b"private")
    stranger = await signed_in(setup, acme.fake, 8)
    with pytest.raises(NotFound):
        await part_uploads.sign(setup, stranger, made.id, [1])
    with pytest.raises(NotFound):
        await part_uploads.finish(setup, stranger, made.id, [])
    with pytest.raises(NotFound):
        await part_uploads.read(setup, stranger, made.id)
    with pytest.raises(NotFound):
        await part_uploads.abort(setup, stranger, made.id)
    assert (await part_uploads.opened(setup, stranger, TaskPlace(sum_task))) == ()


async def test_signing_is_bounded_and_incomplete_parts_refuse_finish(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    made = await _start(setup, acme, sum_task, b"abc")
    owner = await signed_in(setup, acme.fake, 7)
    for numbers in ([0], [2], [-1], []):
        with pytest.raises(InvalidInputs):
            await part_uploads.sign(setup, owner, made.id, numbers)
    await _send(setup, acme, made, b"ab")
    found = await part_uploads.read(setup, owner, made.id)
    refused = await part_uploads.finish(
        setup, owner, made.id, [(part.number, part.etag) for part in found.parts]
    )
    assert refused.status == "refused" and "declared file size" in (refused.reason or "")
    assert acme.fake.objects.multipart == {} and acme.fake.objects.uploads == {}


async def test_size_and_global_open_upload_allowance_are_enforced(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    with pytest.raises(TooLarge):
        await part_uploads.start_task(
            setup,
            acme.ada,
            sum_task,
            path="large.bin",
            digest="f" * 64,
            size=setup.settings.organiser_file_max + 1,
        )
    for index in range(setup.settings.open_part_uploads):
        await _start(setup, acme, sum_task, bytes([index]), path=f"{index}.bin")
    with pytest.raises(UploadLimit):
        await _start(setup, acme, sum_task, b"another", path="another.bin")
    owner = await signed_in(setup, acme.fake, 7)
    opened = await part_uploads.opened(setup, owner, TaskPlace(sum_task))
    await part_uploads.abort(setup, owner, opened[0].id)
    assert (await _start(setup, acme, sum_task, b"another", path="another.bin")).status == "open"


async def test_a_finishing_transfer_interrupted_by_a_restart_expires_on_read(
    setup: Setup, acme: Acme, sum_task: TaskId, clock: FakeClock
) -> None:
    made = await _start(setup, acme, sum_task, b"interrupted")
    async with setup.unit_of_work() as ctx:
        row = await ctx.db.get(PartUpload, made.id)
        assert row is not None
        row.status = "finishing"
        row.deadline_at = ctx.now - timedelta(seconds=1)
    owner = await signed_in(setup, acme.fake, 7)
    expired = await part_uploads.read(setup, owner, made.id)
    assert expired.status == "refused" and "interrupted" in (expired.reason or "")
    assert acme.fake.objects.multipart == {} and acme.fake.objects.uploads == {}


async def test_current_forge_permissions_are_checked_before_start_and_storage(
    setup: Setup, acme: Acme, sum_task: TaskId
) -> None:
    from forge.domain.errors import Forbidden
    from forge.domain.roles import Role, Scope

    made = await _start(setup, acme, sum_task, b"revoked")
    owner = await signed_in(setup, acme.fake, 7)
    await _send(setup, acme, made, b"revoked")
    found = await part_uploads.read(setup, owner, made.id)
    await acme.fake.orgs.revoke_role(7, Scope("acme"), Role.ADMIN)
    with pytest.raises(Forbidden):
        await _start(setup, acme, sum_task, b"new")
    await part_uploads.finish(
        setup, owner, made.id, [(part.number, part.etag) for part in found.parts]
    )
    await setup.settle()
    assert (await part_uploads.read(setup, owner, made.id)).status == "refused"
    assert acme.fake.objects.uploads == {}
