"""The object store over S3, as Garage serves it beside Forgejo. The store is
reached at its internal endpoint for everything the platform does itself:
starting and joining an upload in parts, measuring, reading and removing an
object. What a browser or a grading machine is handed is signed for the
address it uses instead, the platform's public URL or the URL machines reach
it at, under which the proxy passes `/unicon-uploads/` and `/unicon-results/`
to the store with the Host header unchanged, so the signature the store
checks is the one made here. Addresses are path-style, the bucket first.

A browser's file of one request goes by a form post whose policy caps its
length. Garage takes the bucket from the form and not from the URL, so the
form carries `bucket` as a field of its own, which a stock presigned post
leaves out. A presigned PUT cannot cap a length, so each part of a large file
goes to a URL signed with that part's exact length, which the store then
holds the request to. A grading machine's log goes by a presigned PUT of no
set length, so what reads a log back bounds the read instead (`read` with
`max_size`).

boto3 is a blocking client, so each call to the store runs on a worker
thread. Signing makes no call at all.
"""

import asyncio
import hashlib
from collections.abc import Callable, Sequence
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
from forge.port.objects import FinishedPart, Measured, Store, UploadForm

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

CHUNK = 1024 * 1024
MISSING = frozenset({"NoSuchKey", "NoSuchUpload", "NoSuchBucket", "404", "NotFound"})
REFUSED = frozenset({"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML"})
DENIED = frozenset({"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"})


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Where the store is and how the platform signs in to it: the internal
    endpoint, its region, the key, the two buckets, and the two addresses a
    signature is made for, the public URL for browsers and the machine URL
    for grading machines.
    """

    endpoint: str
    region: str
    access_key: str
    secret_key: str = field(repr=False)
    uploads_bucket: str
    results_bucket: str
    public_url: str
    machine_url: str


def _client(config: StorageConfig, endpoint: str) -> S3Client:
    client: S3Client = boto3.client(
        "s3",
        endpoint_url=endpoint.rstrip("/"),
        region_name=config.region,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
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
        self._internal = _client(config, config.endpoint)
        self._public = _client(config, config.public_url)
        self._machines = _client(config, config.machine_url)
        self._buckets = {Store.UPLOADS: config.uploads_bucket, Store.RESULTS: config.results_bucket}

    def upload_form(self, key: str, *, max_size: int, expires_in: timedelta) -> UploadForm:
        bucket = self._buckets[Store.UPLOADS]
        signed = self._public.generate_presigned_post(
            bucket,
            key,
            Fields={"bucket": bucket},
            Conditions=[["content-length-range", 0, max_size]],
            ExpiresIn=int(expires_in.total_seconds()),
        )
        url = str(signed["url"])
        return UploadForm(
            url=url if url.endswith("/") else f"{url}/",
            fields={str(name): str(value) for name, value in signed["fields"].items()},
        )

    async def start_parts(self, key: str) -> str:
        started = await self._call(
            self._internal.create_multipart_upload, Bucket=self._buckets[Store.UPLOADS], Key=key
        )
        return str(started["UploadId"])

    def part_url(
        self, key: str, parts_id: str, number: int, *, length: int, expires_in: timedelta
    ) -> str:
        return self._public.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self._buckets[Store.UPLOADS],
                "Key": key,
                "UploadId": parts_id,
                "PartNumber": number,
                "ContentLength": length,
            },
            ExpiresIn=int(expires_in.total_seconds()),
        )

    async def finish_parts(self, key: str, parts_id: str, parts: Sequence[FinishedPart]) -> None:
        await self._call(
            self._internal.complete_multipart_upload,
            Bucket=self._buckets[Store.UPLOADS],
            Key=key,
            UploadId=parts_id,
            MultipartUpload={
                "Parts": [{"PartNumber": part.number, "ETag": part.etag} for part in parts]
            },
        )

    async def abandon_parts(self, key: str, parts_id: str) -> None:
        try:
            await self._call(
                self._internal.abort_multipart_upload,
                Bucket=self._buckets[Store.UPLOADS],
                Key=key,
                UploadId=parts_id,
            )
        except NotFound:
            return

    def put_url(self, store: Store, key: str, *, expires_in: timedelta) -> str:
        return self._machines.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._buckets[store], "Key": key},
            ExpiresIn=int(expires_in.total_seconds()),
        )

    async def measure(self, store: Store, key: str) -> Measured | None:
        bucket = self._buckets[store]

        def read_through() -> Measured:
            body = self._internal.get_object(Bucket=bucket, Key=key)["Body"]
            digest = hashlib.sha256()
            size = 0
            for chunk in body.iter_chunks(CHUNK):
                digest.update(chunk)
                size += len(chunk)
            return Measured(size=size, sha256=digest.digest())

        try:
            return await self._call(read_through)
        except NotFound:
            return None

    async def read(self, store: Store, key: str, *, max_size: int | None = None) -> bytes:
        bucket = self._buckets[store]

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

    async def delete(self, store: Store, key: str) -> None:
        try:
            await self._call(self._internal.delete_object, Bucket=self._buckets[store], Key=key)
        except NotFound:
            return

    @staticmethod
    async def _call[T](function: Callable[..., T], /, **arguments: Any) -> T:
        try:
            return await asyncio.to_thread(function, **arguments)
        except (ClientError, BotoCoreError) as exc:
            raise _refusal(exc) from exc


class NoStore:
    """The object store of a Forgejo implementation built without one, as a
    test of another area builds it: every call is `Misconfigured`.
    """

    def upload_form(self, key: str, *, max_size: int, expires_in: timedelta) -> UploadForm:
        raise Misconfigured("no object store is configured")

    async def start_parts(self, key: str) -> str:
        raise Misconfigured("no object store is configured")

    def part_url(
        self, key: str, parts_id: str, number: int, *, length: int, expires_in: timedelta
    ) -> str:
        raise Misconfigured("no object store is configured")

    async def finish_parts(self, key: str, parts_id: str, parts: Sequence[FinishedPart]) -> None:
        raise Misconfigured("no object store is configured")

    async def abandon_parts(self, key: str, parts_id: str) -> None:
        raise Misconfigured("no object store is configured")

    def put_url(self, store: Store, key: str, *, expires_in: timedelta) -> str:
        raise Misconfigured("no object store is configured")

    async def measure(self, store: Store, key: str) -> Measured | None:
        raise Misconfigured("no object store is configured")

    async def read(self, store: Store, key: str, *, max_size: int | None = None) -> bytes:
        raise Misconfigured("no object store is configured")

    async def delete(self, store: Store, key: str) -> None:
        raise Misconfigured("no object store is configured")
