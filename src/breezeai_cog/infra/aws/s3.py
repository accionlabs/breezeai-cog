"""Streaming S3 upload: writes NDJSON lines through gzip to an S3 object, concurrently
with production (PassThrough → gzip → multipart), mirroring the JS ``createS3UploadStream``
(``s3-upload.js``). A background thread runs ``upload_fileobj`` reading the pipe while the
caller writes; memory stays bounded. ``ContentType=application/x-ndjson``,
``ContentEncoding=gzip``."""

from __future__ import annotations
import gzip
import os
import threading
from typing import Any, Callable
from threading import Lock
from ...config import Settings
from botocore.exceptions import EndpointConnectionError,ConnectionClosedError,ConnectTimeoutError,ReadTimeoutError
import time
from ..interface import InfraStream

_client: Any | None = None
_client_lock = Lock()
_reconnect_lock = Lock()


def _default_client(settings: Settings) -> Any:
    """
        Creates and returns the shared AWS S3 client.
        Args:
            settings (Settings): AWS configuration and credentials.
        Returns:
            Any: The configured AWS S3 client.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                _client = boto3.client(
                    "s3",
                    region_name=settings.aws_region,
                    **settings.aws_credentials_kwargs,
                )
    return _client


def invalidate_client() -> None:
    """
    Invalidates the shared AWS S3 client.
    This ensures that a new client is created when the S3 client
    is requested again.
    """
    global _client

    with _client_lock:
        _client = None


class AWSStreamUpload(InfraStream):
    """AWS-specific streaming gzip upload to S3."""

    def __init__(
        self,
        key: str,
        settings: Settings,
        *,
        client: Any | None = None,
    ) -> None:
        bucket = settings.aws_s3_bucket

        if not bucket:
            raise RuntimeError("AWS_S3_BUCKET is not configured")

        self._bucket = bucket
        self._key = key
        self._settings = settings
        self._client = client if client is not None else _default_client(settings)
        read_fd, write_fd = os.pipe()

        self._reader = os.fdopen(read_fd, "rb")
        self._writer = os.fdopen(write_fd, "wb")
        self._gz = gzip.GzipFile(
            fileobj=self._writer,
            mode="wb",
        )

        self._error: BaseException | None = None

        self._thread = threading.Thread(
            target=self.upload,
            daemon=True,
        )
        self._thread.start()

    def upload(self) -> None:
        """
            Uploads the gzip-compressed stream to the configured S3 bucket.
            Any exception raised during the upload is stored for handling
            when the stream is closed.
        """
        try:
            self.execute_with_reconnect(
                lambda: self._client.upload_fileobj(
                    self._reader,
                    self._bucket,
                    self._key,
                    ExtraArgs={
                        "ContentType": "application/x-ndjson",
                        "ContentEncoding": "gzip",
                    },
                )
            )
        except BaseException as exc:
            self._error = exc

    def execute_with_reconnect(self,operation: Callable[[], Any]) -> Any: 
        """
            Executes an S3 operation with retry and client reconnection.
            Args:
                operation (Callable[[], Any]): The S3 operation to execute.
            Returns:
                Any: The result returned by the S3 operation.
            Raises:
                ConnectionError: If the operation fails after all retry attempts.
        """
        for attempt in range(self._settings.s3_retry_attempts): 
            try: 
                return operation() 
            except (EndpointConnectionError,ConnectionClosedError,ConnectTimeoutError,ReadTimeoutError): 
                if attempt == self._settings.s3_retry_attempts - 1: 
                    raise 
                failed_client = self._client 
                with _reconnect_lock: 
                    global _client 
                    if _client is failed_client: 
                        invalidate_client() 
                        _client = _default_client(self._settings) 
                    self._client = _client 
            time.sleep(self._settings.s3_retry_wait)

    def write_line(self, line: str) -> None:
        """Write one line to the streaming upload."""
        self._gz.write(line.encode("utf-8"))

    def close(self) -> str:
        """Finish the upload and return the S3 key."""
        self._gz.close()
        self._writer.close()
        self._thread.join()
        self._reader.close()
        if self._error is not None:
            raise self._error
        return self._key

