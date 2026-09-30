"""The in-memory forge's primitives and object store behave as a real forge
and store do: a primitive's declaration is read as the organiser at its
version; and the store refuses a form or a part URL past its expiry or
carrying more than it was signed for.
"""

import hashlib
from datetime import timedelta

import pytest

from forge.domain.clock import FakeClock
from forge.domain.errors import Forbidden, NotFound, Rejected
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import PrimitiveId
from forge.forges.fake import FakeForge
from forge.port.objects import FinishedPart, Store
from forge.testing import PRIMITIVES, seed_primitives


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
