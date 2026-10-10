"""The grading run log store over S3, as Garage serves it beside Forgejo.

This is the only object store the platform signs for. Everything a person
uploads goes into the forge's own large-file store through the upload door
(`adapters.git.forgejo.uploads`) and never passes through here.

A grading machine is handed a presigned PUT signed for the address machines
reach the platform at, under which the proxy passes `/unicon-results/` to
the store with the Host header unchanged, so the signature the store checks
is the one made here. Addresses are path-style, the bucket first. The PUT is
of no set length, so what reads a log back bounds the read instead (`read`
with `max_size`).

boto3 is a blocking client, so each call to the store runs on a worker
thread. Signing makes no call at all.
"""

import asyncio
from collections.abc import Callable
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

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

CHUNK = 1024 * 1024
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
        self._machines = _client(config, config.machine_url)
        self._bucket = config.results_bucket

    def put_url(self, key: str, *, expires_in: timedelta) -> str:
        return self._machines.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=int(expires_in.total_seconds()),
        )

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

    def put_url(self, key: str, *, expires_in: timedelta) -> str:
        raise Misconfigured(NO_STORE)

    async def read(self, key: str, *, max_size: int | None = None) -> bytes:
        raise Misconfigured(NO_STORE)
