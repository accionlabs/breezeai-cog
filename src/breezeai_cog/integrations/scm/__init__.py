"""SCM (git hosting) provider abstraction for ``/api/analyze-diff``.

Mirrors the layout of the sibling ``breezeai-sdlc-autonomous-agents`` SCM package: one
abstract client, one module per provider, a factory with a registry, shared retry and
HTTP plumbing. The operations differ because this service needs source acquisition
(tree / compare / file content / clone URL), not pull-request metadata."""

from .base import SUPPORTED_PROVIDERS, AbstractSCMClient, ChangeSet, CommitInfo, PrComment, PullRequestInfo, RepoRef
from .factory import SCMClientFactory
from .errors import SCMAPIError, SCMCredentialError, SCMError, UnsupportedSCMProviderError
from .repository import parse_repo_url

__all__ = [
    "SUPPORTED_PROVIDERS",
    "AbstractSCMClient",
    "ChangeSet",
    "CommitInfo",
    "PrComment",
    "PullRequestInfo",
    "RepoRef",
    "SCMClientFactory",
    "SCMAPIError",
    "SCMCredentialError",
    "SCMError",
    "UnsupportedSCMProviderError",
    "parse_repo_url",
]
