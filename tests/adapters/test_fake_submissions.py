"""The in-memory forge behaves as a real one does: a submission holds exactly
its files, is numbered after the highest there is, carries its key and takes
the next number when another took its own; a primitive's declaration is read
as the organiser at its version; the upload door keeps only bytes that are
what its address names and only from the credential it was opened with; and
a run log's URL stops working when it expires.
"""

import hashlib
from datetime import timedelta

import pytest

from forge.adapters.fakes import FakeForge
from forge.domain.clock import FakeClock
from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import OrgId, PrimitiveId, TaskId, WorkspaceId
from forge.domain.names import UserOwner
from forge.port.uploads import SubmissionPlace, TaskPlace
from forge.testing import PRIMITIVES, seed_primitives


async def _place(fake: FakeForge) -> tuple[WorkspaceId, TaskId, AsUser]:
    await fake.orgs.create_org(OrgId("acme"), description="Acme")
    contest = await fake.content.create_contest(OrgId("acme"), "spring", {"contest.yaml": b"c"})
    task = await fake.content.create_task(contest, "sum", {"task.yaml": b"t"})
    workspace = await fake.workspaces.open_workspace(contest, UserOwner(8), [8])
    await fake.workspaces.open_submission_place(workspace, task, [8])
    return workspace, task, AsUser(8, fake.mint(8))


async def test_a_submission_holds_exactly_its_files_and_carries_its_key(fake: FakeForge) -> None:
    workspace, task, bob = await _place(fake)

    first = await fake.workspaces.record_submission(
        bob, workspace, task, {"files/a/x.py": b"1", "submission.json": b"{}"}, key="first-key"
    )
    second = await fake.workspaces.record_submission(
        bob, workspace, task, {"files/a/y.py": b"2", "submission.json": b"{}"}, key="second-key"
    )

    assert [
        (made.number, made.key) for made in await fake.workspaces.list_submissions(workspace, task)
    ] == [(1, "first-key"), (2, "second-key")]
    repo = fake.state.repos[("acme", "spring.sum.u8.sub")]
    assert sorted(repo.snapshots[second.version]) == ["files/a/y.py", "submission.json"]
    assert repo.versions["submission/1"] == first.version
    assert repo.history[-1].author_id == 8
    assert (
        await fake.workspaces.read_submission_file(bob, first.id, "files/a/x.py", max_size=1)
        == b"1"
    )
    with pytest.raises(NotFound):
        await fake.workspaces.read_submission_file(bob, second.id, "files/a/x.py", max_size=1)


async def test_a_number_another_took_first_is_taken_by_the_next(fake: FakeForge) -> None:
    workspace, task, bob = await _place(fake)
    fake.racing_submissions = 1

    made = await fake.workspaces.record_submission(
        bob, workspace, task, {"submission.json": b"{}"}, key="racing-key"
    )

    assert made.number == 2
    listed = await fake.workspaces.list_submissions(workspace, task)
    assert [(entry.number, entry.key) for entry in listed] == [(1, None), (2, "racing-key")]


async def test_a_lost_answer_leaves_the_submission_named(fake: FakeForge) -> None:
    workspace, task, bob = await _place(fake)
    fake.lose_submission_answer = True

    with pytest.raises(Unavailable):
        await fake.workspaces.record_submission(
            bob, workspace, task, {"submission.json": b"{}"}, key="lost-key-1"
        )

    [made] = await fake.workspaces.list_submissions(workspace, task)
    assert made.key == "lost-key-1"


async def test_a_declaration_is_read_as_the_organiser_at_its_version(fake: FakeForge) -> None:
    await seed_primitives(fake)
    fake.primitives.add("scorer", {"v1": b"one", "v2": b"two"})
    ada = AsUser(7, fake.mint(7))

    assert (
        await fake.primitives.read_declaration(ada, PrimitiveId("compile"), "v2")
        == (PRIMITIVES["compile"])
    )
    assert await fake.primitives.read_declaration(ada, PrimitiveId("scorer"), "v1") == b"one"
    assert fake.calls_to("read_declaration")[0].identity == ada
    with pytest.raises(NotFound):
        await fake.primitives.read_declaration(PLATFORM, PrimitiveId("compile"), "v9")


async def test_the_door_takes_only_the_bytes_its_address_names(fake: FakeForge) -> None:
    workspace, task, bob = await _place(fake)
    place = SubmissionPlace(workspace, task)
    content = b"the model's weights"
    digest, size = hashlib.sha256(content).hexdigest(), len(content)

    assert not await fake.uploads.holds(place, as_=bob, digest=digest, size=size)
    door = fake.uploads.door(place, as_=bob, digest=digest, size=size)

    # The forge hashes what arrives and keeps nothing that is not what the
    # address names, so a claim on its own buys nothing.
    with pytest.raises(Rejected):
        fake.uploads.send(door.path, door.authorization, b"x" * size)
    with pytest.raises(Rejected):
        fake.uploads.send(door.path, door.authorization, content + b"!")
    assert not await fake.uploads.holds(place, as_=bob, digest=digest, size=size)

    fake.uploads.send(door.path, door.authorization, content)
    assert await fake.uploads.holds(place, as_=bob, digest=digest, size=size)
    # Sending it again after a dropped line is the same upload, not a second.
    fake.uploads.send(door.path, door.authorization, content)


async def test_an_address_is_no_use_without_the_credential_it_came_with(
    fake: FakeForge,
) -> None:
    workspace, task, bob = await _place(fake)
    place = SubmissionPlace(workspace, task)
    content = b"private"
    digest, size = hashlib.sha256(content).hexdigest(), len(content)
    door = fake.uploads.door(place, as_=bob, digest=digest, size=size)

    with pytest.raises(Forbidden):
        fake.uploads.send(door.path, "Basic someone-else", content)
    with pytest.raises(Forbidden):
        fake.uploads.send("/made/up.git/info/lfs/objects/x/1", door.authorization, content)
    assert not await fake.uploads.holds(place, as_=bob, digest=digest, size=size)


async def test_an_object_belongs_to_the_place_it_was_sent_to(fake: FakeForge) -> None:
    workspace, task, bob = await _place(fake)
    mine, task_repo = SubmissionPlace(workspace, task), TaskPlace(task)
    content = b"shared bytes"
    digest, size = hashlib.sha256(content).hexdigest(), len(content)

    door = fake.uploads.door(mine, as_=bob, digest=digest, size=size)
    fake.uploads.send(door.path, door.authorization, content)

    assert await fake.uploads.holds(mine, as_=bob, digest=digest, size=size)
    assert not await fake.uploads.holds(task_repo, as_=bob, digest=digest, size=size)


async def test_a_machine_writes_a_result_through_its_url(fake: FakeForge) -> None:
    url = fake.objects.put_url("logs/g/1.log", expires_in=timedelta(hours=1))

    fake.objects.put(url, b"log")

    assert url.startswith("http://machines.test/unicon-results/logs/g/1.log")
    assert await fake.objects.read("logs/g/1.log") == b"log"
    assert await fake.objects.read("logs/g/1.log", max_size=3) == b"log"
    with pytest.raises(Rejected):
        await fake.objects.read("logs/g/1.log", max_size=2)


async def test_a_result_url_stops_working_when_it_expires(
    fake: FakeForge, clock: FakeClock
) -> None:
    url = fake.objects.put_url("logs/g/2.log", expires_in=timedelta(hours=1))

    clock.advance(timedelta(hours=1, seconds=1))

    with pytest.raises(Forbidden):
        fake.objects.put(url, b"late")
    with pytest.raises(NotFound):
        await fake.objects.read("logs/g/2.log")
