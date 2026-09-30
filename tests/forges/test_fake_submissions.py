"""The in-memory forge's submissions, primitives and object store behave as a
real forge and store do: a submission holds exactly its files, is numbered
after the highest there is, carries its key and takes the next number when
another took its own; a primitive's declaration is read as the organiser at
its version; and the store refuses a form or a part URL past its expiry or
carrying more than it was signed for.
"""

import hashlib
from datetime import timedelta

import pytest

from forge.domain.clock import FakeClock
from forge.domain.errors import Forbidden, NotFound, Rejected, Unavailable
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import OrgName, PrimitiveId, TaskId, WorkspaceId
from forge.domain.names import UserOwner
from forge.forges.fake import FakeForge
from forge.port.objects import FinishedPart, Store
from forge.testing import PRIMITIVES, seed_primitives


async def _place(fake: FakeForge) -> tuple[WorkspaceId, TaskId, AsUser]:
    await fake.orgs.create_org(OrgName("acme"), description="Acme")
    contest = await fake.content.create_contest(OrgName("acme"), "spring", {"contest.yaml": b"c"})
    task = await fake.content.create_task(contest, "sum", {"task.yaml": b"t"})
    workspace = await fake.workspaces.open_workspace(contest, UserOwner("bob"), [8])
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
    repo = fake.state.repos[("acme", "spring.sum.bob.sub")]
    assert sorted(repo.snapshots[second.version]) == ["files/a/y.py", "submission.json"]
    assert repo.versions["submission/1"] == first.version
    assert repo.history[-1].author_id == 8
    assert await fake.workspaces.read_submission_file(bob, first.id, "files/a/x.py") == b"1"
    with pytest.raises(NotFound):
        await fake.workspaces.read_submission_file(bob, second.id, "files/a/x.py")


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
        await fake.primitives.read_declaration(ada, PrimitiveId("compile"), "v1")
        == (PRIMITIVES["compile"])
    )
    assert await fake.primitives.read_declaration(ada, PrimitiveId("scorer"), "v1") == b"one"
    assert fake.calls_to("read_declaration")[0].identity == ada
    with pytest.raises(NotFound):
        await fake.primitives.read_declaration(PLATFORM, PrimitiveId("compile"), "v9")


async def test_a_form_takes_a_file_within_its_size_until_it_expires(
    fake: FakeForge, clock: FakeClock
) -> None:
    form = fake.objects.upload_form("uploads/1", max_size=3, expires_in=timedelta(minutes=15))

    with pytest.raises(Rejected):
        fake.objects.post(form.fields, b"four")
    fake.objects.post(form.fields, b"abc")
    measured = await fake.objects.measure(Store.UPLOADS, "uploads/1")
    assert measured is not None
    assert (measured.size, measured.sha256) == (3, hashlib.sha256(b"abc").digest())

    clock.advance(timedelta(minutes=15))
    with pytest.raises(Forbidden):
        fake.objects.post(form.fields, b"ab")
    assert await fake.objects.measure(Store.UPLOADS, "uploads/2") is None


async def test_parts_take_exactly_their_length_and_join_only_as_they_arrived(
    fake: FakeForge,
) -> None:
    parts = await fake.objects.start_parts("uploads/big")
    ttl = timedelta(hours=1)
    one = fake.objects.part_url("uploads/big", parts, 1, length=2, expires_in=ttl)
    two = fake.objects.part_url("uploads/big", parts, 2, length=1, expires_in=ttl)

    with pytest.raises(Forbidden):
        fake.objects.put_part(one, b"abc")
    first = fake.objects.put_part(one, b"ab")
    second = fake.objects.put_part(two, b"c")
    with pytest.raises(Rejected):
        await fake.objects.finish_parts(
            "uploads/big", parts, [FinishedPart(1, first), FinishedPart(2, "wrong")]
        )
    await fake.objects.finish_parts(
        "uploads/big", parts, [FinishedPart(1, first), FinishedPart(2, second)]
    )

    assert await fake.objects.read(Store.UPLOADS, "uploads/big") == b"abc"
    with pytest.raises(NotFound):
        await fake.objects.finish_parts("uploads/big", parts, [FinishedPart(1, first)])
    await fake.objects.delete(Store.UPLOADS, "uploads/big")
    await fake.objects.delete(Store.UPLOADS, "uploads/big")
    with pytest.raises(NotFound):
        await fake.objects.read(Store.UPLOADS, "uploads/big")


async def test_a_machine_writes_a_result_through_its_url(fake: FakeForge) -> None:
    url = fake.objects.put_url(Store.RESULTS, "logs/g/1.log", expires_in=timedelta(hours=1))

    fake.objects.put(url, b"log")

    assert url.startswith("http://machines.test/unicon-results/logs/g/1.log")
    assert await fake.objects.read(Store.RESULTS, "logs/g/1.log") == b"log"
    assert await fake.objects.read(Store.RESULTS, "logs/g/1.log", max_size=3) == b"log"
    with pytest.raises(Rejected):
        await fake.objects.read(Store.RESULTS, "logs/g/1.log", max_size=2)
