from breezeai_cog.config import Settings
from breezeai_cog.infra.provider_type import ProviderType


class ProviderConfig:
    """Resolves and provides the active cloud provider."""

    def __init__(self, provider: ProviderType) -> None:
        self._provider = provider

    @property
    def is_active(self) -> ProviderType:
        """Return the validated active cloud provider."""
        return self._provider

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProviderConfig":
        """Create provider configuration from application settings."""
        return cls(
            ProviderType(settings.infra_provider)
        )
