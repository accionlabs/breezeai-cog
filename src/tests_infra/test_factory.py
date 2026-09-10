from unittest.mock import patch
from breezeai_cog.infra.factory import ProviderFactory
from breezeai_cog.infra.provider_type import ProviderType
import pytest

def test_aws_provider_creates_aws_stream():
    factory = ProviderFactory(ProviderType.AWS)

    with patch("breezeai_cog.infra.factory.AWSStreamUpload") as mock_stream:
        factory.create_stream("test.json", None)

        mock_stream.assert_called_once_with("test.json", None)

def test_unsupported_provider_raises_error():
    factory = ProviderFactory("unsupported")

    with pytest.raises(RuntimeError):
        factory.create_stream("test.json", None)