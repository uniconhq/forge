"""The grading results store over S3, as Garage serves it beside Forgejo,
and the read-only way into Forgejo's own large-file bucket.

Everything a person uploads goes into the forge's own large-file store
through the upload door (`adapters.git.forgejo.uploads`) and never passes
through here.

A grading machine is handed presigned links signed for the address machines
reach the platform at, under which the proxy passes `/unicon-results/` and
`/forgejo-lfs/` to the store with the Host header unchanged, so the
signature the store checks is the one made here. Addresses are path-style,
the bucket first. A PUT is of no set length, so what reads an object back
bounds the read instead (`read` with `max_size`). A PUT signed for a
SHA-256 carries it as `ChecksumSHA256`, so `x-amz-checksum-sha256` is
among the signed headers: Garage refuses the PUT without it, and a body
not of it, and gives it back on a HEAD asked for checksums. A large-file object is
signed with a second key, one that may only read Forgejo's bucket, at the
name Forgejo gives it: `MINIO_BASE_PATH`, `lfs/`, then the SHA-256 split as
`<h[:2]>/<h[2:4]>/<h[4:]>` (Forgejo's `Pointer.RelativePath`).

boto3 is a blocking client, so each call to the store runs on a worker
thread. Signing makes no call at all.
"""

import asyncio
import base64
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from forge.domain.errors import (
    Misconfigured,
    NotFound,
    PortError,
    Rejected,
    Unavailable,
)
from forge.port.objects import Part, Stored

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

CHUNK = 1024 * 1024
LFS_BASE = "lfs/"
"""Forgejo's `[lfs] MINIO_BASE_PATH`, deploy's `forgejo.ini`."""
SHA256 = re.compile(r"[0-9a-f]{64}")
NO_STORE = "the platform is running without an object store"
MISSING = frozenset({"NoSuchKey", "NoSuchUpload", "NoSuchBucket", "404", "NotFound"})
REFUSED = frozenset({"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML"})
DENIED = frozenset({"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"})


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Where the store is and how the platform signs in to it: the internal
    endpoint, its region, the key, the bucket run logs go in, and the
    address a grading machine reaches the platform at, which is what a
    signature for it is made for.
    """

    endpoint: str
    region: str
    access_key: str
    secret_key: str = field(repr=False)
    results_bucket: str
    machine_url: str
    lfs_access_key: str
    lfs_secret_key: str = field(repr=False)
    lfs_bucket: str
    uploads_bucket: str = "unicon-uploads"
    public_url: str | None = None


def lfs_object(digest: str) -> str:
    """The name Forgejo's store keeps the large-file object of SHA-256
    `digest` under."""
    if not SHA256.fullmatch(digest):
        raise ValueError("a large-file object is named by a SHA-256 in hex")
    return f"{LFS_BASE}{digest[:2]}/{digest[2:4]}/{digest[4:]}"


def _client(
    config: StorageConfig, endpoint: str, access_key: str, secret_key: str, *, short: bool = False
) -> S3Client:
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        region_name=config.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3 if short else 5,
            read_timeout=5 if short else 60,
            retries={"total_max_attempts": 1 if short else 4, "mode": "standard"},
        ),
    )
    return client


def _refusal(exc: Exception) -> PortError:
    """What a failure of the store is, as one of the port's errors."""
    if isinstance(exc, ClientError):
        error: dict[str, Any] = dict(exc.response.get("Error") or {})
        code = str(error.get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        if code in MISSING or status == 404:
            return NotFound(f"the store has no such object ({code})")
        if code in REFUSED or status == 400:
            return Rejected(f"the store refused the request ({code})")
        if code in DENIED or status == 403:
            return Misconfigured(f"the store refused the platform's key ({code})")
        return Unavailable(f"the store answered {status or code}")
    return Unavailable(f"no answer from the store: {type(exc).__name__}")


class S3Objects:
    def __init__(self, config: StorageConfig) -> None:
        self._internal = _client(config, config.endpoint, config.access_key, config.secret_key)
        self._machines = _client(config, config.machine_url, config.access_key, config.secret_key)
        self._lfs = _client(
            config, config.machine_url, config.lfs_access_key, config.lfs_secret_key
        )
        self._uploads = _client(
            config, config.public_url or config.machine_url, config.access_key, config.secret_key
        )
        self._upload_internal = _client(
            config, config.endpoint, config.access_key, config.secret_key, short=True
        )
        self._uploads_bucket = config.uploads_bucket
        self._bucket = config.results_bucket
        self._lfs_bucket = config.lfs_bucket

    def put_url(self, key: str, *, expires_in: timedelta, sha256: str | None = None) -> str:
        params = {"Bucket": self._bucket, "Key": key}
        if sha256 is not None:
            params["ChecksumSHA256"] = base64.b64encode(bytes.fromhex(sha256)).decode()
        return self._machines.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=int(expires_in.total_seconds())
        )

    def get_url(self, key: str, *, expires_in: timedelta) -> str:
        return self._machines.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=int(expires_in.total_seconds()),
        )

    def lfs_get_url(self, digest: str, *, expires_in: timedelta) -> str:
        return self._lfs.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._lfs_bucket, "Key": lfs_object(digest)},
            ExpiresIn=int(expires_in.total_seconds()),
        )

    async def head(self, key: str) -> Stored:
        got = await self._call(
            self._internal.head_object, Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"
        )
        checksum = got.get("ChecksumSHA256")
        try:
            sha256 = base64.b64decode(checksum, validate=True).hex() if checksum else None
        except ValueError:
            sha256 = None
        return Stored(int(got["ContentLength"]), sha256)

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        bucket = self._bucket

        def read_all() -> bytes:
            got = self._internal.get_object(Bucket=bucket, Key=key)
            body = got["Body"]
            try:
                if max_size is None:
                    return body.read()
                if int(got.get("ContentLength") or 0) > max_size:
                    raise Rejected(f"the object is larger than {max_size} bytes")
                content = body.read(max_size + 1)
            finally:
                body.close()
            if len(content) > max_size:
                raise Rejected(f"the object is larger than {max_size} bytes")
            return content

        return await self._call(read_all)

    async def create_multipart(self, key: str) -> str:
        made = await self._call(
            self._upload_internal.create_multipart_upload, Bucket=self._uploads_bucket, Key=key
        )
        return str(made["UploadId"])

    def part_url(self, key: str, upload: str, number: int, *, expires_in: timedelta) -> str:
        return self._uploads.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self._uploads_bucket,
                "Key": key,
                "UploadId": upload,
                "PartNumber": number,
            },
            ExpiresIn=int(expires_in.total_seconds()),
        )

    async def list_parts(self, key: str, upload: str) -> list[Part]:
        found: list[Part] = []
        marker = 0
        while True:
            page = await self._call(
                self._upload_internal.list_parts,
                Bucket=self._uploads_bucket,
                Key=key,
                UploadId=upload,
                PartNumberMarker=marker,
            )
            found.extend(
                Part(int(part["PartNumber"]), str(part["ETag"]), int(part["Size"]))
                for part in page.get("Parts", [])
            )
            if not page.get("IsTruncated"):
                return found
            next_marker = int(page["NextPartNumberMarker"])
            if next_marker <= marker:
                raise Unavailable("the store's parts page did not advance")
            marker = next_marker

    async def complete_multipart(self, key: str, upload: str, parts: Sequence[Part]) -> None:
        # Joining a large upload takes the store a while: the normal client's
        # wait, not the short one's.
        await self._call(
            self._internal.complete_multipart_upload,
            Bucket=self._uploads_bucket,
            Key=key,
            UploadId=upload,
            MultipartUpload={
                "Parts": [{"ETag": part.etag, "PartNumber": part.number} for part in parts]
            },
        )

    async def abort_multipart(self, key: str, upload: str) -> None:
        with suppress(NotFound):
            await self._call(
                self._upload_internal.abort_multipart_upload,
                Bucket=self._uploads_bucket,
                Key=key,
                UploadId=upload,
            )

    async def head_upload(self, key: str) -> int:
        got = await self._call(
            self._upload_internal.head_object, Bucket=self._uploads_bucket, Key=key
        )
        return int(got["ContentLength"])

    async def stream_upload(self, key: str) -> AsyncGenerator[bytes]:
        got = await self._call(
            self._upload_internal.get_object, Bucket=self._uploads_bucket, Key=key
        )
        body = got["Body"]
        try:
            while chunk := await self._call(lambda: body.read(CHUNK)):
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    async def delete_upload(self, key: str) -> None:
        await self._call(self._upload_internal.delete_object, Bucket=self._uploads_bucket, Key=key)

    @staticmethod
    async def _call[T](function: Callable[..., T], /, **arguments: Any) -> T:
        try:
            return await asyncio.to_thread(function, **arguments)
        except (ClientError, BotoCoreError) as exc:
            raise _refusal(exc) from exc


class NoStore:
    """The object store of a forge joined without one, as a test of another
    area joins it: every call is `Misconfigured`.
    """

    def put_url(self, key: str, *, expires_in: timedelta, sha256: str | None = None) -> str:
        raise Misconfigured(NO_STORE)

    def get_url(self, key: str, *, expires_in: timedelta) -> str:
        raise Misconfigured(NO_STORE)

    def lfs_get_url(self, digest: str, *, expires_in: timedelta) -> str:
        raise Misconfigured(NO_STORE)

    async def head(self, key: str) -> Stored:
        raise Misconfigured(NO_STORE)

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        raise Misconfigured(NO_STORE)

    async def create_multipart(self, key: str) -> str:
        raise Misconfigured(NO_STORE)

    def part_url(self, key: str, upload: str, number: int, *, expires_in: timedelta) -> str:
        raise Misconfigured(NO_STORE)

    async def list_parts(self, key: str, upload: str) -> list[Part]:
        raise Misconfigured(NO_STORE)

    async def complete_multipart(self, key: str, upload: str, parts: Sequence[Part]) -> None:
        raise Misconfigured(NO_STORE)

    async def abort_multipart(self, key: str, upload: str) -> None:
        raise Misconfigured(NO_STORE)

    async def head_upload(self, key: str) -> int:
        raise Misconfigured(NO_STORE)

    def stream_upload(self, key: str) -> AsyncGenerator[bytes]:
        raise Misconfigured(NO_STORE)

    async def delete_upload(self, key: str) -> None:
        raise Misconfigured(NO_STORE)
