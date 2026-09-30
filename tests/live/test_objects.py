"""The object store over S3 against a running Garage, named by
`UNICON_LIVE_S3_ENDPOINT` with the platform's key in `UNICON_LIVE_S3_ACCESS_KEY`
and `UNICON_LIVE_S3_SECRET_KEY`, and its `unicon-uploads` and `unicon-results`
buckets made. The forms and URLs are signed for the endpoint itself, where a
deployment signs them for the proxy in front of it. A form takes a file within
its policy and refuses a larger one and a late one; each part of a large file
takes exactly the length it was signed for; what arrived is measured, read and
removed; and a grading machine writes a result through its URL, which a
read bounded below its length refuses.
"""

import asyncio
import hashlib
import os
import secrets
from datetime import timedelta

import httpx
import pytest

from forge.domain.errors import Rejected
from forge.forges.forgejo.objects import S3Objects, StorageConfig
from forge.port.objects import FinishedPart, Store

ENDPOINT = os.environ.get("UNICON_LIVE_S3_ENDPOINT")
ACCESS_KEY = os.environ.get("UNICON_LIVE_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("UNICON_LIVE_S3_SECRET_KEY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (ENDPOINT and ACCESS_KEY and SECRET_KEY), reason="no live object store configured"
    ),
]

MEGABYTE = 1024 * 1024


@pytest.fixture
def store() -> S3Objects:
    assert ENDPOINT and ACCESS_KEY and SECRET_KEY
    return S3Objects(
        StorageConfig(
            endpoint=ENDPOINT,
            region="garage",
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            uploads_bucket="unicon-uploads",
            results_bucket="unicon-results",
            public_url=ENDPOINT,
            machine_url=ENDPOINT,
        )
    )


def _post(url: str, fields: dict[str, str], content: bytes) -> httpx.Response:
    return httpx.post(url, data=fields, files={"file": ("f", content)}, timeout=30)


async def test_a_form_takes_a_file_within_its_policy_and_refuses_a_larger_or_late_one(
    store: S3Objects,
) -> None:
    key = f"uploads/live-{secrets.token_hex(4)}"
    form = store.upload_form(key, max_size=5, expires_in=timedelta(minutes=5))
    try:
        assert form.url.endswith("/unicon-uploads/")
        assert _post(form.url, dict(form.fields), b"123456").status_code >= 400
        assert await store.measure(Store.UPLOADS, key) is None

        taken = _post(form.url, dict(form.fields), b"12345")
        assert taken.status_code in (200, 201, 204), taken.text
        measured = await store.measure(Store.UPLOADS, key)
        assert measured is not None
        assert (measured.size, measured.sha256) == (5, hashlib.sha256(b"12345").digest())
        assert await store.read(Store.UPLOADS, key) == b"12345"

        late = store.upload_form(f"{key}-late", max_size=5, expires_in=timedelta(seconds=1))
        await asyncio.sleep(2)
        assert _post(late.url, dict(late.fields), b"1").status_code >= 400
        assert await store.measure(Store.UPLOADS, f"{key}-late") is None
    finally:
        await store.delete(Store.UPLOADS, key)
    assert await store.measure(Store.UPLOADS, key) is None
    await store.delete(Store.UPLOADS, key)


async def test_each_part_takes_exactly_its_signed_length_and_the_parts_join(
    store: S3Objects,
) -> None:
    key = f"uploads/live-parts-{secrets.token_hex(4)}"
    content = b"a" * (5 * MEGABYTE) + b"b"
    parts_id = await store.start_parts(key)
    ttl = timedelta(minutes=10)
    first = store.part_url(key, parts_id, 1, length=5 * MEGABYTE, expires_in=ttl)
    second = store.part_url(key, parts_id, 2, length=1, expires_in=ttl)
    try:
        with httpx.Client(timeout=60) as client:
            assert client.put(second, content=b"bb").status_code >= 400
            one = client.put(first, content=content[: 5 * MEGABYTE])
            two = client.put(second, content=b"b")
        assert one.status_code == two.status_code == 200
        await store.finish_parts(
            key,
            parts_id,
            [FinishedPart(1, one.headers["etag"]), FinishedPart(2, two.headers["etag"])],
        )
        measured = await store.measure(Store.UPLOADS, key)
        assert measured is not None
        assert (measured.size, measured.sha256) == (len(content), hashlib.sha256(content).digest())
    finally:
        await store.abandon_parts(key, parts_id)
        await store.delete(Store.UPLOADS, key)


async def test_a_grading_machine_writes_a_result_through_its_url(store: S3Objects) -> None:
    key = f"logs/live-{secrets.token_hex(4)}/1.log"
    url = store.put_url(Store.RESULTS, key, expires_in=timedelta(minutes=5))
    try:
        assert httpx.put(url, content=b"the log", timeout=30).status_code == 200
        assert await store.read(Store.RESULTS, key) == b"the log"
        assert await store.read(Store.RESULTS, key, max_size=7) == b"the log"
        with pytest.raises(Rejected):
            await store.read(Store.RESULTS, key, max_size=6)
        assert httpx.put(url, content=b"", timeout=30).status_code == 200
        assert await store.read(Store.RESULTS, key, max_size=0) == b""
    finally:
        await store.delete(Store.RESULTS, key)
