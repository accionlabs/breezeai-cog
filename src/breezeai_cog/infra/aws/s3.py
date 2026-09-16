"""Streaming S3 upload: writes NDJSON lines through gzip to an S3 object
while a background thread uploads the stream using upload_fileobj.
The pipe-based design keeps memory usage bounded.

ContentType=application/x-ndjson and ContentEncoding=gzip are set
for the uploaded S3 object.
"""

from __future__ import annotations

import gzip
import os
import threading
from threading import Lock
from typing import Any

from botocore.config import Config

from breezeai_cog.config import Settings
from breezeai_cog.infra.interface import InfraStream

_client: Any | None = None
_client_lock = Lock()
 
 
def _default_client(settings: Settings) -> Any:
    """
    Creates and returns the shared AWS S3 client.
 
    Args:
        settings (Settings): AWS configuration and credentials.
    Returns:
        Any: The configured AWS S3 client.
    """
    global _client
 
    with _client_lock:
        if _client is None:
            import boto3
 
            config = Config(
                retries={
                    "max_attempts": settings.s3_retry_attempts,
                    "mode": "adaptive",
                },
                connect_timeout=settings.s3_connect_timeout,
                read_timeout=settings.s3_read_timeout,
            )
 
            _client = boto3.client(
                "s3",
                region_name=settings.aws_region,
                config=config,
                **settings.aws_credentials_kwargs,
            )
 
        return _client
 


class AWSStreamUpload(InfraStream):
    def __init__(self,key: str,settings: Settings,*,client: Any | None = None) -> None:
        bucket = settings.aws_s3_bucket
 
        if not bucket:
            raise ValueError("AWS_S3_BUCKET is not configured")
 
        if settings.s3_retry_attempts < 1:
            raise ValueError("s3_retry_attempts must be at least 1")
 
        self._bucket = bucket
        self._key = key
        self._settings = settings
        self._client = client if client is not None else _default_client(settings)
 
        read_fd, write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "rb")
        self._writer = os.fdopen(write_fd, "wb")
        self._gz = gzip.GzipFile(fileobj=self._writer, mode="wb")
 
        self._error: Exception | None = None
        self._error_lock = Lock()
 
        self._closed = False
        self._close_lock = Lock()
 
        self._thread = threading.Thread(
            target=self._run_upload,
            name=f"s3-upload-{key}",
            daemon=True,
        )
        self._thread.start()
 
    def __enter__(self) -> "AWSStreamUpload":
            return self
    
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            if exc_type is not None:
                # Caller failed before finishing the stream (e.g. mid-batch of
                # write_line calls). Don't try to complete the upload — abort
                # the pipe so the background thread unblocks and exits cleanly.
                self._abort()
                return
            self.close()
    
    def _set_error(self, exc: Exception) -> None:
            with self._error_lock:
                if self._error is None:
                    self._error = exc
    
    def _get_error(self) -> Exception | None:
            with self._error_lock:
                return self._error

    def _run_upload(self) -> None:
        """Background worker. The only thing that reads self._reader."""
        try:
            self._client.upload_fileobj(
                self._reader,
                self._bucket,
                self._key,
                ExtraArgs={
                    "ContentType": "application/x-ndjson",
                    "ContentEncoding": "gzip",
                },
            )
        except BaseException as exc:
            self._set_error(exc)
        finally:
            try:
                self._reader.close()
            except OSError:
                pass


    def upload(self) -> None:
        """
        Wait for the background upload to finish and raise
        any error that occurred during the upload.
        """
        self._thread.join()

        error = self._get_error()
        if error is not None:
            raise error


    def write_line(self, line: str) -> None:
        """Write one line to the streaming upload."""
        if self._closed:
            raise RuntimeError("Cannot write to a closed stream")

        error = self._get_error()
        if error is not None:
            raise error

        try:
            self._gz.write(line.encode("utf-8"))
        except (BrokenPipeError, OSError) as exc:
            error = self._get_error()
            if error is not None:
                raise error from exc
            raise   

    def close(self) -> str:
        """Finish the upload and return the S3 key."""
        with self._close_lock:
            if self._closed:
                error = self._get_error()
                if error is not None:
                    raise error
                return self._key

            self._closed = True

            try:
                self._gz.close()  # writes the gzip trailer
            except Exception as exc:
                error = self._get_error()
                if error is not None:
                    raise error from exc
                raise
            finally:
                try:
                    self._writer.close()  # sends EOF to the reader
                except OSError:
                    pass

        # Wait for the background upload to finish.
        self.upload()

        return self._key     
    
    def _abort(self) -> None:
            """Best-effort cleanup when the stream is abandoned without close()."""
            with self._close_lock:
                if self._closed:
                    return
    
                self._closed = True
    
                try:
                    self._writer.close()
                except OSError:
                    pass
    
            self._thread.join()