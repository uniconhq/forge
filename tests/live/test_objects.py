"""The grading results store over S3 against a running Garage, named by
`UNICON_LIVE_S3_ENDPOINT` with the platform's key in
`UNICON_LIVE_S3_ACCESS_KEY` and `UNICON_LIVE_S3_SECRET_KEY`, and its
`unicon-results` bucket made. The URL is signed for the endpoint itself,
where a deployment signs it for the proxy in front of it.

What people upload goes into the forge's own large-file store through the
upload door, which `findings/upload-door-test.md` measures against a
running Forgejo. With `UNICON_LIVE_LFS_DIGEST`, the SHA-256 of an object
that Forgejo wrote into `forgejo-lfs` (an upload's pointer names it), and
the read-only key bootstrap makes for it in `UNICON_LIVE_S3_LFS_ACCESS_KEY`
and `UNICON_LIVE_S3_LFS_SECRET_KEY`, the link the forge signs for that hash
reads exactly that object, so the name it works out is the one Forgejo
wrote.
"""

import base64
import hashlib
import os
import secrets
from datetime import timedelta

import httpx
import pytest

from forge.adapters.objects.s3 import S3Objects, StorageConfig
from forge.domain.errors import NotFound, Rejected
from forge.port.objects import Stored

ENDPOINT = os.environ.get("UNICON_LIVE_S3_ENDPOINT")
ACCESS_KEY = os.environ.get("UNICON_LIVE_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("UNICON_LIVE_S3_SECRET_KEY")
LFS_ACCESS_KEY = os.environ.get("UNICON_LIVE_S3_LFS_ACCESS_KEY")
LFS_SECRET_KEY = os.environ.get("UNICON_LIVE_S3_LFS_SECRET_KEY")
LFS_DIGEST = os.environ.get("UNICON_LIVE_LFS_DIGEST")

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
            lfs_access_key=LFS_ACCESS_KEY or ACCESS_KEY,
            lfs_secret_key=LFS_SECRET_KEY or SECRET_KEY,
            lfs_bucket="forgejo-lfs",
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


async def test_an_earlier_steps_output_is_read_by_its_link_and_by_nothing_else(
    store: S3Objects,
) -> None:
    key = f"results/{secrets.token_hex(8)}/steps/solution/out/1/output"
    httpx.put(store.put_url(key, expires_in=timedelta(hours=1)), content=b"42\n", timeout=30)

    read = httpx.get(store.get_url(key, expires_in=timedelta(minutes=15)), timeout=30)
    other = httpx.get(
        store.get_url(key, expires_in=timedelta(minutes=15)).replace("/out/1/", "/out/2/"),
        timeout=30,
    )

    assert read.status_code == 200 and read.content == b"42\n"
    assert other.status_code in (400, 403), other.text


async def test_a_put_signed_for_a_checksum_writes_only_those_bytes(store: S3Objects) -> None:
    key = f"results/{secrets.token_hex(8)}/steps/compile/outputs.json"
    digest = hashlib.sha256(b"{}").hexdigest()
    url = store.put_url(key, expires_in=timedelta(hours=1), sha256=digest)
    checksum = {"x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(digest)).decode()}

    other = httpx.put(url, content=b"[]", headers=checksum, timeout=30)
    bare = httpx.put(url, content=b"{}", timeout=30)
    with pytest.raises(NotFound):
        await store.head(key)
    written = httpx.put(url, content=b"{}", headers=checksum, timeout=30)

    assert other.status_code == 400 and bare.status_code in (400, 403), (other.text, bare.text)
    assert written.status_code == 200, written.text
    assert await store.head(key) == Stored(2, digest)


@pytest.mark.skipif(
    not (LFS_DIGEST and LFS_ACCESS_KEY and LFS_SECRET_KEY),
    reason="no object Forgejo wrote, or no read-only key for its store, named",
)
async def test_the_large_file_link_reads_the_object_forgejo_wrote(store: S3Objects) -> None:
    assert LFS_DIGEST
    read = httpx.get(store.lfs_get_url(LFS_DIGEST, expires_in=timedelta(minutes=15)), timeout=30)

    assert read.status_code == 200, read.text
    assert hashlib.sha256(read.content).hexdigest() == LFS_DIGEST
