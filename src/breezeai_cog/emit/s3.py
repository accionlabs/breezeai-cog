"""Streaming S3 upload: writes NDJSON lines through gzip to an S3 object, concurrently
with production (PassThrough → gzip → multipart), mirroring the JS ``createS3UploadStream``
(``s3-upload.js``). A background thread runs ``upload_fileobj`` reading the pipe while the
caller writes; memory stays bounded. ``ContentType=application/x-ndjson``,
``ContentEncoding=gzip``."""

from __future__ import annotations

import gzip
import os
import threading
from typing import Any

from ..config import Settings


class S3StreamUpload:
    """One streaming gzip upload to ``s3://{bucket}/{key}``. Write lines, then ``close``."""

    def __init__(self, key: str, settings: Settings, *, client: Any | None = None) -> None:
        bucket = settings.aws_s3_bucket
        if not bucket:
            raise RuntimeError("AWS_S3_BUCKET is not configured")
        self._bucket = bucket
        self._key = key
        self._client = client or _default_client(settings)

        read_fd, write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "rb")
        self._writer = os.fdopen(write_fd, "wb")
        self._gz = gzip.GzipFile(fileobj=self._writer, mode="wb")
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._upload, daemon=True)
        self._thread.start()

    def _upload(self) -> None:
        try:
            self._client.upload_fileobj(
                self._reader, self._bucket, self._key,
                ExtraArgs={"ContentType": "application/x-ndjson", "ContentEncoding": "gzip"},
            )
        except BaseException as exc:  # surfaced on close()
            self._error = exc

    def write_line(self, line: str) -> None:
        self._gz.write(line.encode("utf-8"))

    def close(self) -> str:
        self._gz.close()
        self._writer.close()
        self._thread.join()
        self._reader.close()
        if self._error is not None:
            raise self._error
        return self._key


def _default_client(settings: Settings) -> Any:
    import boto3

    # Credentials come from boto3's default provider chain (IRSA in-cluster)
    # unless static keys are explicitly configured; see settings.
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        **settings.aws_credentials_kwargs,
    )
