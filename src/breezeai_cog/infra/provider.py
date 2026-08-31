from .factory import ProviderFactory
from .interface import InfraStream
from .provider_config import provider_conf
from ..config import Settings


def open_stream(key: str, settings: Settings) -> InfraStream:
    """Return a streaming storage implementation for the active provider."""

    return ProviderFactory(
        provider_conf.is_active
    ).create_stream(key, settings)