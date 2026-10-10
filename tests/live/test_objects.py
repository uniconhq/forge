"""The grading run log store over S3 against a running Garage, named by
`UNICON_LIVE_S3_ENDPOINT` with the platform's key in
`UNICON_LIVE_S3_ACCESS_KEY` and `UNICON_LIVE_S3_SECRET_KEY`, and its
`unicon-results` bucket made. The URL is signed for the endpoint itself,
where a deployment signs it for the proxy in front of it.

This is the only object store the platform signs for. What people upload
goes into the forge's own large-file store through the upload door, which
`findings/upload-door-test.md` measures against a running Forgejo.
"""

import os
import secrets
from datetime import timedelta

import httpx
import pytest

from forge.adapters.objects.s3 import S3Objects, StorageConfig
from forge.domain.errors import NotFound, Rejected

ENDPOINT = os.environ.get("UNICON_LIVE_S3_ENDPOINT")
ACCESS_KEY = os.environ.get("UNICON_LIVE_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("UNICON_LIVE_S3_SECRET_KEY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (ENDPOINT and ACCESS_KEY and SECRET_KEY), reason="no live object store configured"
    ),
]


@pytest.fixture
def store() -> S3Objects:
    assert ENDPOINT and ACCESS_KEY and SECRET_KEY
    return S3Objects(
        StorageConfig(
            endpoint=ENDPOINT,
            region="garage",
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
            results_bucket="unicon-results",
            machine_url=ENDPOINT,
        )
    )


async def test_a_grading_machine_writes_a_result_through_its_url(store: S3Objects) -> None:
    key = f"logs/{secrets.token_hex(8)}/1.log"
    url = store.put_url(key, expires_in=timedelta(hours=1))

    written = httpx.put(url, content=b"the log", timeout=30)

    assert written.status_code == 200, written.text
    assert await store.read(key) == b"the log"
    assert await store.read(key, max_size=7) == b"the log"
    # A log is read under a bound, since the URL it was written with takes
    # any length and nothing checked one on the way in.
    with pytest.raises(Rejected):
        await store.read(key, max_size=6)


async def test_a_log_that_was_never_written_is_no_log(store: S3Objects) -> None:
    with pytest.raises(NotFound):
        await store.read(f"logs/{secrets.token_hex(8)}/1.log")


async def test_a_url_stops_working_when_it_expires(store: S3Objects) -> None:
    key = f"logs/{secrets.token_hex(8)}/1.log"
    url = store.put_url(key, expires_in=timedelta(seconds=-1))

    late = httpx.put(url, content=b"too late", timeout=30)

    # Garage answers an expired presigned URL 400 ("Date is too old"), where
    # AWS answers 403; either way nothing is stored.
    assert late.status_code in (400, 403), late.text
    with pytest.raises(NotFound):
        await store.read(key)
