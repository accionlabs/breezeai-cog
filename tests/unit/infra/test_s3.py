import gzip

import pytest
from unittest.mock import Mock
from breezeai_cog.config import Settings
from breezeai_cog.infra.aws.s3 import AWSStreamUpload


def create_test_settings():
    return Settings(
        aws_s3_bucket="test-bucket",
        aws_region="us-east-1",
    )


class ReadingS3Client:
    """Fake S3 client that drains the pipe like boto3 does.

    ``Mock`` never touches the fileobj, so it leaves the pipe undrained and
    cannot catch truncation, blocking, or corrupt-payload bugs. This fake
    reads to EOF, so the uploaded bytes can be asserted on directly.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.body = b""
        self.calls: list[tuple[str, str, dict | None]] = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):  # noqa: N803
        self.body = fileobj.read()  # blocks until close() sends EOF
        if self.error is not None:
            raise self.error
        self.calls.append((bucket, key, ExtraArgs))


def test_write_line():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    stream.write_line('{"name": "test"}')
    stream.close()

    mock_client.upload_fileobj.assert_called_once()

def test_close():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    result = stream.close()

    assert result == "test.json"


def test_upload_calls_s3():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    stream.write_line('{"name": "test"}')
    stream.close()

    mock_client.upload_fileobj.assert_called_once()


def test_upload_uses_correct_s3_arguments():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    stream.write_line('{"name": "test"}')
    stream.close()

    args, kwargs = mock_client.upload_fileobj.call_args

    assert args[1] == "test-bucket"
    assert args[2] == "test.json"

    assert kwargs["ExtraArgs"]["ContentType"] == "application/x-ndjson"
    assert kwargs["ExtraArgs"]["ContentEncoding"] == "gzip"


def test_zero_retry_attempts_raises_error():
    settings = create_test_settings()
    settings.storage_retry_attempts = 0

    mock_client = Mock()

    with pytest.raises(
        ValueError,
        match="storage_retry_attempts must be at least 1",
    ):
        AWSStreamUpload(
            "test.json",
            settings,
            client=mock_client,
        )


def test_missing_bucket_raises_error():
    settings = create_test_settings()
    settings.aws_s3_bucket = None

    mock_client = Mock()

    with pytest.raises(
        ValueError,
        match="AWS_S3_BUCKET is not configured",
    ):
        AWSStreamUpload(
            "test.json",
            settings,
            client=mock_client,
        )


def test_upload_error_is_raised_on_close():
    settings = create_test_settings()
    mock_client = Mock()

    mock_client.upload_fileobj.side_effect = RuntimeError("S3 upload failed")

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    with pytest.raises(RuntimeError, match="S3 upload failed"):
        stream.close()


def test_write_after_close_raises_error():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    stream.close()

    with pytest.raises(
        RuntimeError,
        match="Cannot write to a closed stream",
    ):
        stream.write_line('{"name": "test"}')


def test_close_twice_does_not_upload_twice():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    assert stream.close() == "test.json"
    assert stream.close() == "test.json"

    mock_client.upload_fileobj.assert_called_once()

# ── Tests using the reading fake (real pipe semantics) ────────────────────────


@pytest.mark.timeout(30)
def test_uploaded_bytes_gunzip_to_exactly_what_was_written():
    """The object stored in S3 must be a complete, valid gzip of the lines."""
    settings = create_test_settings()
    client = ReadingS3Client()

    lines = [f'{{"i": {i}}}\n' for i in range(50)]

    stream = AWSStreamUpload("test.json", settings, client=client)
    for line in lines:
        stream.write_line(line)

    assert stream.close() == "test.json"

    # Would fail with EOFError if the gzip trailer were missing.
    assert gzip.decompress(client.body).decode("utf-8") == "".join(lines)


@pytest.mark.timeout(60)
def test_payload_larger_than_pipe_buffer_does_not_block():
    """Streaming must work past the ~64KB kernel pipe buffer.

    With a client that never reads, write_line would block forever once the
    buffer filled; this proves the background thread genuinely drains it.
    """
    settings = create_test_settings()
    client = ReadingS3Client()

    line = ('{"payload": "' + "x" * 500 + '"}\n')
    count = 400  # ~200KB uncompressed, well past the pipe buffer

    stream = AWSStreamUpload("big.json", settings, client=client)
    for _ in range(count):
        stream.write_line(line)
    stream.close()

    assert gzip.decompress(client.body).decode("utf-8") == line * count


@pytest.mark.timeout(30)
def test_error_while_reading_stream_propagates_on_close():
    """A failure inside upload_fileobj must surface from close(), never be
    swallowed — the caller must not receive a key for an incomplete object."""
    settings = create_test_settings()
    client = ReadingS3Client(error=ConnectionError("connection reset"))

    stream = AWSStreamUpload("test.json", settings, client=client)
    stream.write_line('{"a": 1}\n')

    with pytest.raises(ConnectionError, match="connection reset"):
        stream.close()
