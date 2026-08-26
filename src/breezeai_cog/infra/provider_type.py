from enum import Enum
# This is the ProviderType Enum class that has the list of provider that are supported 
class ProviderType(str, Enum):
    """Identifies the cloud provider selected for service initialization."""
    AWS = "aws"
    