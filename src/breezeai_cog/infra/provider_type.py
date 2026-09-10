from enum import Enum
class ProviderType(str, Enum):
    """Identifies the cloud provider selected for service initialization."""
    AWS = "aws"
    