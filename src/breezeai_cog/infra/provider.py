from breezeai_cog.infra.factory import ProviderFactory
from breezeai_cog.infra.interface import InfraStream
from breezeai_cog.infra.provider_config import ProviderConfig
from breezeai_cog.config import Settings


def open_stream(key: str, settings: Settings) -> InfraStream:
    """Return a streaming storage implementation for the active provider."""

    provider_conf = ProviderConfig.from_settings(settings)

    return ProviderFactory(
        provider_conf.is_active
    ).create_stream(key, settings)