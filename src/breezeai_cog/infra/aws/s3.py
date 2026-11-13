"""Streaming S3 upload: writes NDJSON lines through gzip to an S3 object
while a background thread uploads the stream using upload_fileobj.
A bounded in-process queue connects the writer to the uploader, which keeps
memory usage bounded and behaves the same on every OS (an ``os.pipe`` raises
``EINVAL`` instead of ``BrokenPipeError`` on Windows when the reader is gone).

ContentType=application/x-ndjson and ContentEncoding=gzip are set
for the uploaded S3 object.
"""

from __future__ import annotations

import gzip
import io
import queue
import threading
from threading import Lock
from typing import Any

from botocore.config import Config

from ...config import Settings
from ..interface import InfraStream

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
                    "max_attempts": settings.storage_retry_attempts,
                    "mode": "adaptive",
                },
                connect_timeout=settings.storage_connect_timeout,
                read_timeout=settings.storage_read_timeout,
            )

            _client = boto3.client(
                "s3",
                region_name=settings.aws_region,
                config=config,
                **settings.aws_credentials_kwargs,
            )

        return _client


_EOF = object()
_QUEUE_CHUNKS = 64  # bounded: backpressure on the writer, never unbounded memory


class _QueueReader(io.RawIOBase):
    """Read end: blocks until the writer sends data or EOF."""

    def __init__(self, chunks: queue.Queue[Any]) -> None:
        super().__init__()
        self._chunks = chunks
        self._buf = b""
        self._eof = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts: list[bytes] = []
            while True:
                part = self.read(1 << 20)
                if not part:
                    return b"".join(parts)
                parts.append(part)
        while not self._buf and not self._eof:
            chunk = self._chunks.get()
            if chunk is _EOF:
                self._eof = True
            else:
                self._buf = chunk
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


class _QueueWriter(io.RawIOBase):
    """Write end: blocks when the queue is full; drops data once the reader is closed."""

    def __init__(self, chunks: queue.Queue[Any], reader: _QueueReader) -> None:
        super().__init__()
        self._chunks = chunks
        self._reader = reader

    def writable(self) -> bool:
        return True

    def _put(self, item: Any) -> None:
        while not self._reader.closed:
            try:
                self._chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def write(self, data: Any) -> int:
        data = bytes(data)
        if data:
            self._put(data)
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._put(_EOF)
        super().close()


class AWSStreamUpload(InfraStream):
    def __init__(self, key: str, settings: Settings, *, client: Any | None = None) -> None:
        bucket = settings.aws_s3_bucket

        if not bucket:
            raise ValueError("AWS_S3_BUCKET is not configured")

        if settings.storage_retry_attempts < 1:
            raise ValueError("storage_retry_attempts must be at least 1")

        self._bucket = bucket
        self._key = key
        self._settings = settings
        self._client = client if client is not None else _default_client(settings)

        chunks: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_CHUNKS)
        self._reader = _QueueReader(chunks)
        self._writer = _QueueWriter(chunks, self._reader)
        self._gz = gzip.GzipFile(fileobj=self._writer, mode="wb")

        self._error: BaseException | None = None
        self._error_lock = Lock()

        self._closed = False
        self._close_lock = Lock()

        self._thread = threading.Thread(
            target=self._run_upload,
            name=f"s3-upload-{key}",
            daemon=True,
        )
        self._thread.start()

    def _set_error(self, exc: BaseException) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = exc

    def _get_error(self) -> BaseException | None:
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

    def _wait_for_upload(self) -> None:
        """
        Wait for the background upload to finish and raise
        any error that occurred during the upload.

        The caller MUST have closed self._writer first; otherwise the
        background thread never sees EOF and this blocks forever.
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
        self._wait_for_upload()

        return self._key
