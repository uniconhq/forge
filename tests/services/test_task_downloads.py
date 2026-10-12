"""A task's own files downloaded by its organisers through the download door:
any file of the task, typed or uploaded, at the version a read of it named,
for whoever holds a role reaching the task, observer or above, at the task,
its contest or its org. The door is where at the forge the proxy reads the
file whole and the credential it presents there; the bytes never pass
through the platform. Someone holding no role there, an entrant included,
is refused before any door opens, and so are a path that is not plain and a
version that is not a commit's id.
"""

import hashlib

import pytest

from forge.domain.errors import Forbidden, InvalidPath, NotFound
from forge.domain.ids import TaskId, VersionId
from forge.domain.roles import Role, Scope
from forge.domain.uploads import Door
from forge.runtime.setup import Setup
from forge.services import access, files, roles, uploads
from tests.services.conftest import Acme, Entered, make_task, organiser, signed_in

DATA = b"a test's answer, pretend it is gigabytes\n" * 64
PATH = "data/answer.bin"


async def _uploaded(setup: Setup, acme: Acme, task: TaskId, path: str = PATH) -> None:
    """An upload saved into the task at `path`, the way an organiser's
    browser sends one.
    """
    digest = hashlib.sha256(DATA).hexdigest()
    slot = await uploads.task_file_slot(
        setup, acme.ada, task, path=path, size=len(DATA), sha256=digest
    )
    session = await signed_in(setup, acme.fake, 7)
    door = await uploads.door(setup, session, slot.id, length=len(DATA))
    acme.fake.uploads.send(door.path, door.authorization, DATA)
    await uploads.complete(setup, session, task, slot.id)
    await files.write_upload(setup, acme.ada, task, path, slot.id, None)


async def _version(setup: Setup, acme: Acme, task: TaskId, path: str) -> VersionId:
    """The version a read of the file names, as the page that offers the
    download read it.
    """
    read = await files.read(setup, acme.ada, task, path)
    assert read.version is not None
    return read.version


@pytest.mark.parametrize(
    ("scope", "role"),
    [
        (Scope("acme", "spring", "sum"), Role.OBSERVER),
        (Scope("acme", "spring"), Role.OBSERVER),
        (Scope("acme"), Role.OBSERVER),
        (Scope("acme", "spring"), Role.MANAGER),
    ],
    ids=["task observer", "contest observer", "org observer", "contest manager"],
)
async def test_whoever_holds_a_role_reaching_the_task_downloads_its_files(
    setup: Setup, acme: Acme, entered: Entered, scope: Scope, role: Role
) -> None:
    await _uploaded(setup, acme, entered.task)
    acme.fake.add_user(33, "obi")
    await roles.grant(setup, acme.ada, scope, "obi", role)
    obi = await organiser(setup, acme.fake, 33, Scope("acme", "spring", "sum"))

    typed = await files.download(
        setup,
        obi,
        entered.task,
        "task.yaml",
        await _version(setup, acme, entered.task, "task.yaml"),
    )
    upload = await files.download(
        setup, obi, entered.task, PATH, await _version(setup, acme, entered.task, PATH)
    )

    head = await acme.fake.content.read_file(acme.ada.identity, entered.task, "task.yaml")
    assert await acme.fake.content.fetch(typed) == head.content
    # The upload's own bytes, never the pointer its commit holds.
    assert await acme.fake.content.fetch(upload) == DATA
    assert upload.path.endswith(
        f"/media/{PATH}?ref={await _version(setup, acme, entered.task, PATH)}"
    )


async def test_a_download_reads_the_file_as_it_was_at_the_version_named(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    await files.write(setup, acme.ada, entered.task, "notes.txt", b"first\n", None)
    first = await _version(setup, acme, entered.task, "notes.txt")
    current = await files.read(setup, acme.ada, entered.task, "notes.txt")
    await files.write(setup, acme.ada, entered.task, "notes.txt", b"second\n", current.token)
    second = await _version(setup, acme, entered.task, "notes.txt")

    then = await files.download(setup, acme.ada, entered.task, "notes.txt", first)
    now = await files.download(setup, acme.ada, entered.task, "notes.txt", second)
    at = await files.read(setup, acme.ada, entered.task, "notes.txt", first)

    assert first != second
    assert at.version == first
    assert await acme.fake.content.fetch(then) == b"first\n"
    assert await acme.fake.content.fetch(now) == b"second\n"


async def test_the_compiled_plans_are_downloaded_as_any_other_file(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    # Observers read plans/ through the file reads already; the door is the
    # same read, whole.
    version = await _version(setup, acme, entered.task, "plans/plan.json")

    door = await files.download(setup, acme.ada, entered.task, "plans/plan.json", version)

    plan = await acme.fake.content.read_file(acme.ada.identity, entered.task, "plans/plan.json")
    assert await acme.fake.content.fetch(door) == plan.content


async def test_an_entrant_and_a_stranger_reach_no_door(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    acme.fake.add_user(31, "dee")
    stranger = await signed_in(setup, acme.fake, 31)

    for session in (entered.session, stranger):
        with pytest.raises(Forbidden):
            await access.organiser_at(setup, session, Role.OBSERVER, "acme", "spring", "sum")
        with pytest.raises(Forbidden):
            await access.organiser(setup, session, Scope("acme", "spring", "sum"), Role.OBSERVER)
    assert acme.fake.calls_to("media_door") == []


async def test_a_role_at_another_task_opens_no_door_to_this_one(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    other = await make_task(setup, acme, "other")
    acme.fake.add_user(33, "obi")
    await roles.grant(setup, acme.ada, Scope("acme", "spring", "other"), "obi", Role.MANAGER)
    obi = await organiser(setup, acme.fake, 33, Scope("acme", "spring", "other"))
    version = await _version(setup, acme, entered.task, "task.yaml")

    with pytest.raises(Forbidden):
        await files.download(setup, obi, entered.task, "task.yaml", version)
    assert other != entered.task
    assert acme.fake.calls_to("media_door") == []


@pytest.mark.parametrize(
    "path",
    ["", "../sum/task.yaml", "tests/../task.yaml", "tests//1", "/task.yaml", "a%2Fb", "a?b"],
    ids=["empty", "up", "up inside", "empty part", "absolute", "escaped", "a query"],
)
async def test_a_path_that_is_not_plain_opens_no_door(
    setup: Setup, acme: Acme, entered: Entered, path: str
) -> None:
    version = await _version(setup, acme, entered.task, "task.yaml")

    with pytest.raises(InvalidPath):
        await files.download(setup, acme.ada, entered.task, path, version)
    assert acme.fake.calls_to("media_door") == []


@pytest.mark.parametrize(
    "version",
    ["main", "HEAD", "published/1", "0123abc", "A" * 40, "0" * 41, "0" * 39, ""],
    ids=["a branch", "head", "a publication", "short", "upper case", "41", "39", "empty"],
)
async def test_a_version_that_is_not_a_commits_id_opens_no_door(
    setup: Setup, acme: Acme, entered: Entered, version: str
) -> None:
    # The forge's `ref` would read a name as a branch or a tag first.
    with pytest.raises(NotFound):
        await files.download(setup, acme.ada, entered.task, "task.yaml", VersionId(version))
    assert acme.fake.calls_to("media_door") == []


async def test_a_file_or_a_version_the_task_does_not_have_is_the_forges_refusal(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    version = await _version(setup, acme, entered.task, "task.yaml")
    missing = await files.download(setup, acme.ada, entered.task, "nothing.txt", version)
    elsewhere = await files.download(
        setup, acme.ada, entered.task, "task.yaml", VersionId("f" * 40)
    )

    for door in (missing, elsewhere):
        with pytest.raises(NotFound):
            await acme.fake.content.fetch(door)


async def test_a_door_is_read_with_its_own_credential_only(
    setup: Setup, acme: Acme, entered: Entered
) -> None:
    version = await _version(setup, acme, entered.task, "task.yaml")
    door = await files.download(setup, acme.ada, entered.task, "task.yaml", version)

    with pytest.raises(Forbidden):
        await acme.fake.content.fetch(Door(door.path, "fake another"))
