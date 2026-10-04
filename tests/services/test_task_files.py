"""An organiser's file into a task: the slot, the door, and the save that
writes the pointer beside their other files and the compiled plans.

The same door a contestant's file goes through, with the task itself as the
place and the manager role in front of it. The save is where it lands: an
edit naming an upload becomes the pointer to it, and the upload is marked as
taken by that save. A file you type or edit is content; a file you upload is
an upload, and the two never become each other.
"""

import hashlib
from typing import Any

import pytest

from forge.domain.content import Edit, Uploaded
from forge.domain.errors import Forbidden, InvalidInputs, UploadLimit, UploadNotReady
from forge.domain.ids import TaskId
from forge.domain.roles import Role, Scope
from forge.domain.uploads import UploadStatus, pointer_text
from forge.port.uploads import TaskPlace
from forge.runtime.setup import Setup
from forge.services import files, publications, roles, uploads
from tests.services.conftest import Acme, Entered, organiser, signed_in

DATA = b"a dataset, pretend it is large\n" * 32
PATH = "data/weights.bin"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _slot(setup: Setup, acme: Acme, task: TaskId, path: str = PATH) -> uploads.Slot:
    slot: uploads.Slot = await uploads.task_file_slot(
        setup, acme.ada, task, path=path, size=len(DATA), sha256=_digest(DATA)
    )
    return slot


async def _send(setup: Setup, acme: Acme, slot: uploads.Slot, task: TaskId) -> None:
    """The bytes through the door, as the organiser's browser sends them."""
    session = await signed_in(setup, acme.fake, 7)
    door = await uploads.door(setup, session, slot.id, length=len(DATA))
    acme.fake.uploads.send(door.path, door.authorization, DATA)
    await uploads.complete(setup, session, task, slot.id)


async def test_an_organisers_file_reaches_the_task_as_a_pointer(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await _slot(setup, acme, entered.task)
    await _send(setup, acme, slot, entered.task)

    saved = await files.write_upload(setup, acme.ada, entered.task, PATH, slot.id, None)

    assert isinstance(saved, publications.Published | publications.Draft), saved
    written = await acme.fake.content.read_file(acme.ada.identity, entered.task, PATH)
    assert written.content == pointer_text(_digest(DATA), len(DATA))
    # The bytes are the forge's; the commit names them and holds none of them.
    assert DATA not in written.content
    assert await acme.fake.uploads.holds(
        TaskPlace(entered.task), as_=acme.ada.identity, digest=_digest(DATA), size=len(DATA)
    )


async def test_a_save_marks_the_upload_it_took(setup: Setup, acme: Acme, entered: Entered) -> None:
    slot = await _slot(setup, acme, entered.task)
    await _send(setup, acme, slot, entered.task)

    await files.write_upload(setup, acme.ada, entered.task, PATH, slot.id, None)

    session = await signed_in(setup, acme.fake, 7)
    taken = await uploads.complete(setup, session, entered.task, slot.id)
    assert taken.status is UploadStatus.CONSUMED
    # Taken once: the same upload cannot be written a second time.
    with pytest.raises(InvalidInputs):
        await files.write_upload(setup, acme.ada, entered.task, PATH, slot.id, None)


async def test_an_upload_is_written_only_where_it_was_asked_for(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await _slot(setup, acme, entered.task)
    await _send(setup, acme, slot, entered.task)

    with pytest.raises(InvalidInputs) as refused:
        await files.write_upload(setup, acme.ada, entered.task, "data/elsewhere.bin", slot.id, None)

    assert "data/weights.bin" in refused.value.detail


async def test_an_upload_that_has_not_arrived_cannot_be_saved(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await _slot(setup, acme, entered.task)

    with pytest.raises(UploadNotReady):
        await files.write_upload(setup, acme.ada, entered.task, PATH, slot.id, None)

    # Nothing of the save landed.
    listed = await acme.fake.content.list_files(acme.ada.identity, entered.task)
    assert PATH not in listed.tokens


async def test_nobody_elses_upload_and_no_slot_without_the_role(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await _slot(setup, acme, entered.task)
    await _send(setup, acme, slot, entered.task)
    acme.fake.add_user(33, "obi")
    await roles.grant(setup, acme.ada, Scope("acme", "spring"), "obi", Role.OBSERVER)
    observer = await organiser(setup, acme.fake, 33, Scope("acme", "spring"), Role.OBSERVER)

    with pytest.raises(Forbidden):
        await uploads.task_file_slot(
            setup, observer, entered.task, path=PATH, size=len(DATA), sha256=_digest(DATA)
        )


async def test_the_door_refuses_a_task_file_to_anyone_else(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    slot = await _slot(setup, acme, entered.task)
    acme.fake.add_user(31, "dee")
    stranger = await signed_in(setup, acme.fake, 31)

    with pytest.raises(Forbidden):
        await uploads.door(setup, stranger, slot.id, length=len(DATA))


async def test_an_organisers_files_are_bounded_like_a_contestants(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a bound, a manager could fill the store every contest shares.
    from forge.domain import uploads as rules

    monkeypatch.setattr(rules, "OPEN_MAX", 2)
    for name in ("one.bin", "two.bin"):
        await uploads.task_file_slot(
            setup,
            acme.ada,
            entered.task,
            path=f"data/{name}",
            size=len(DATA),
            sha256=_digest(name.encode()),
        )

    with pytest.raises(UploadLimit):
        await uploads.task_file_slot(
            setup,
            acme.ada,
            entered.task,
            path="data/three.bin",
            size=len(DATA),
            sha256=_digest(b"three"),
        )


async def test_a_file_of_rules_about_files_is_refused_wherever_it_is_typed(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # A place that carries its own rules has every pointer in it stored as a
    # second object by the forge's file API, so every file reads back as 130
    # bytes of text instead of itself.
    for path in (".gitattributes", "data/.gitattributes"):
        with pytest.raises(InvalidInputs):
            await files.write(setup, acme.ada, entered.task, path, b"* filter=lfs\n", None)


async def test_typed_content_that_would_read_as_a_pointer_is_refused(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    forged = pointer_text("a" * 64, 4096)

    with pytest.raises(InvalidInputs):
        await publications.save(
            setup, acme.ada, entered.task, {"data/forged.bin": Edit(forged, None)}
        )


async def test_a_pointer_to_an_object_the_task_holds_may_be_written_back(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # A rollback, or a file read and saved back unchanged, writes the pointer
    # the task's history already has; the object behind it is the task's own.
    slot = await _slot(setup, acme, entered.task)
    await _send(setup, acme, slot, entered.task)
    await files.write_upload(setup, acme.ada, entered.task, PATH, slot.id, None)
    pointer = pointer_text(_digest(DATA), len(DATA))

    await files.write(setup, acme.ada, entered.task, "data/again.bin", pointer, None)

    written = await acme.fake.content.read_file(acme.ada.identity, entered.task, "data/again.bin")
    assert written.content == pointer
    with pytest.raises(InvalidInputs):
        await files.write(setup, acme.ada, entered.task, ".gitattributes", pointer, None)


async def test_an_upload_names_a_file_the_place_can_hold(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    for path in ("data/ends with a space ", "data/" + "x" * 300):
        with pytest.raises(InvalidInputs):
            await uploads.task_file_slot(
                setup, acme.ada, entered.task, path=path, size=len(DATA), sha256=_digest(DATA)
            )


async def test_an_edit_naming_an_upload_that_is_not_the_savers_is_refused(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    import uuid as _uuid

    with pytest.raises(InvalidInputs):
        await publications.save(
            setup,
            acme.ada,
            entered.task,
            {PATH: Edit(Uploaded(_uuid.uuid4()), None)},
        )


async def test_a_failing_forge_leaves_no_row_for_a_task_file(
    setup: Setup, acme: Acme, entered: Entered, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.domain.errors import Unavailable

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise Unavailable("the forge answered 503")

    monkeypatch.setattr(acme.fake.uploads, "holds", down)

    with pytest.raises(Unavailable):
        await _slot(setup, acme, entered.task)
