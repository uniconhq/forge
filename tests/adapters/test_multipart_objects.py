"""Multipart calls keep exact metadata and stream without reading whole objects."""

import io
from contextlib import aclosing
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from botocore.response import StreamingBody
from botocore.stub import Stubber

from forge.adapters.objects.s3 import S3Objects, StorageConfig
from forge.port.objects import Part


def store() -> S3Objects:
    return S3Objects(
        StorageConfig(
            endpoint="http://storage.test",
            region="garage",
            access_key="test",
            secret_key="test",
            results_bucket="results",
            machine_url="http://machine.test",
            lfs_access_key="test",
            lfs_secret_key="test",
            lfs_bucket="lfs",
            public_url="http://browser.test",
        )
    )


async def test_the_upload_bucket_paging_etags_and_public_signer() -> None:
    objects = store()
    client = objects._upload_internal
    params = {"Bucket": "unicon-uploads", "Key": "one"}
    with Stubber(client) as stub:
        stub.add_response("create_multipart_upload", {"UploadId": "upload"}, params)
        assert await objects.create_multipart("one") == "upload"
        stub.add_response(
            "list_parts",
            {
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
                "Parts": [{"PartNumber": 1, "ETag": '"first"', "Size": 5}],
            },
            {**params, "UploadId": "upload", "PartNumberMarker": 0},
        )
        stub.add_response(
            "list_parts",
            {"IsTruncated": False, "Parts": [{"PartNumber": 2, "ETag": '"last"', "Size": 3}]},
            {**params, "UploadId": "upload", "PartNumberMarker": 1},
        )
        parts = await objects.list_parts("one", "upload")
        assert parts == [Part(1, '"first"', 5), Part(2, '"last"', 3)]
        signed = urlsplit(objects.part_url("one", "upload", 2, expires_in=timedelta(hours=1)))
        assert signed.netloc == "browser.test" and signed.path == "/unicon-uploads/one"
        assert parse_qs(signed.query)["partNumber"] == ["2"]
        stub.add_response(
            "complete_multipart_upload",
            {},
            {
                **params,
                "UploadId": "upload",
                "MultipartUpload": {
                    "Parts": [
                        {"PartNumber": 1, "ETag": '"first"'},
                        {"PartNumber": 2, "ETag": '"last"'},
                    ]
                },
            },
        )
        await objects.complete_multipart("one", "upload", parts)
        stub.add_response("abort_multipart_upload", {}, {**params, "UploadId": "upload"})
        await objects.abort_multipart("one", "upload")
        stub.add_response("delete_object", {}, params)
        await objects.delete_upload("one")
        stub.assert_no_pending_responses()


async def test_a_stream_is_bounded_and_its_body_is_closed_when_the_reader_stops() -> None:
    objects = store()
    source = io.BytesIO(b"x" * (3 * 1024 * 1024))
    body = StreamingBody(source, 3 * 1024 * 1024)
    with Stubber(objects._upload_internal) as stub:
        stub.add_response("get_object", {"Body": body}, {"Bucket": "unicon-uploads", "Key": "one"})
        async with aclosing(objects.stream_upload("one")) as chunks:
            assert len(await anext(chunks)) == 1024 * 1024
            assert source.tell() == 1024 * 1024
        assert source.closed
