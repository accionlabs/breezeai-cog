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

    # Verify that data was written to the internal stream
    assert stream._writer is not None

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