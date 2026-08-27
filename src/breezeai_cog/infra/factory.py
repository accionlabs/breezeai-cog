from src.breezeai_cog.infra.interface import InfraStream
from src.breezeai_cog.infra.provider_type import ProviderType
from src.breezeai_cog.infra.aws.s3 import AWSStreamUpload
from ..config import Settings


class ProviderFactory:
    """Factory for creating cloud-specific infrastructure stream implementations."""
    def __init__(self, provider: ProviderType) -> None:
        self.provider = provider

    def create_stream(self,key: str,settings: Settings) -> InfraStream:
        """
            Creates a stream implementation for the configured cloud provider.
            Args:
                key (str): The object key used for the stream.
                settings (Settings): Configuration required to create the stream.
            Returns:
                InfraStream: A cloud-specific stream implementation.
            Raises:
                RuntimeError: If the configured cloud provider is not supported.
        """
        match self.provider:
            case ProviderType.AWS:
                return AWSStreamUpload(key, settings)
            case _:
                raise RuntimeError(
                    f"Unsupported cloud provider: {self.provider}"
                )