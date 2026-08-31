from unittest.mock import Mock, patch

from breezeai_cog.infra.provider import open_stream


def test_open_stream_routes_to_factory():
    mock_stream = Mock()

    with patch(
        "breezeai_cog.infra.provider.ProviderFactory"
    ) as mock_factory:

        mock_factory.return_value.create_stream.return_value = mock_stream

        result = open_stream("test.json", Mock())

        mock_factory.assert_called_once()
        mock_factory.return_value.create_stream.assert_called_once()

        assert result is mock_stream