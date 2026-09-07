import pytest
from unittest.mock import Mock

from breezeai_cog.config import Settings
from breezeai_cog.infra.aws.s3 import AWSStreamUpload


def create_test_settings():
    return Settings(
        aws_s3_bucket="test-bucket",
        aws_region="us-east-1",
        aws_credentials_kwargs={},
    )


def test_write_line():
    settings = create_test_settings()
    mock_client = Mock()

    stream = AWSStreamUpload(
        "test.json",
        settings,
        client=mock_client,
    )

    stream.write_line('{"name": "test"}')

    assert stream._writer is not None

    stream.close()


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
    settings.s3_retry_attempts = 0

    mock_client = Mock()

    with pytest.raises(
        ValueError,
        match="s3_retry_attempts must be at least 1",
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