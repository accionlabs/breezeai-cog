from src.breezeai_cog.config import settings
from src.breezeai_cog.infra.provider_type import ProviderType


class ProviderConfig:
    """Resolves and provides the active cloud provider."""

    def __init__(self, provider: ProviderType) -> None:
        self._provider = provider

    @property
    def is_active(self) -> ProviderType:
        """Return the validated active cloud provider."""
        return self._provider

    @classmethod
    def from_settings(cls) -> "ProviderConfig":
        """Create provider configuration from application settings."""
        return cls(
            ProviderType(settings.infra_provider)
        )


provider_conf = ProviderConfig.from_settings()