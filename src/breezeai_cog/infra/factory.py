from src.breezeai_cog.infra.interface import InfraStream
from src.breezeai_cog.infra.provider_type import ProviderType
from src.breezeai_cog.infra.aws.s3 import AWSStreamUpload
from ..config import Settings


class ProviderFactory:

    def __init__(self, provider: ProviderType) -> None:
        self.provider = provider

    def create_stream(
        self,
        key: str,
        settings: Settings,
    ) -> InfraStream:

        match self.provider:
            case ProviderType.AWS:
                return AWSStreamUpload(key, settings)

            case _:
                raise RuntimeError(
                    f"Unsupported cloud provider: {self.provider}"
                )