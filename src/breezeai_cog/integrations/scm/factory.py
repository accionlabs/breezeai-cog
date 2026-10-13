"""Build the right provider client for a parsed repository URL.

Adding a provider means adding one module and one registry entry; nothing in
``server/git.py`` changes. Classes are imported lazily by dotted path so importing the
package does not import every provider.

Credential resolution, in order:

1. The request body's ``gitToken`` — the existing ``/api/analyze-diff`` contract.
2. ``settings.scm_token_<provider>`` — a deployment-wide fallback per provider.
3. Nothing — anonymous. Public repositories still work; the client logs a warning.

API base resolution: a self-hosted host (anything but the provider's public cloud) is
looked up in ``settings.scm_instance_base_url_mapping``. An unmapped host falls back to
the provider's ``<provider>_api_base_url`` with a warning, because for GitHub Enterprise,
GitLab and Bitbucket Server the API lives on a different path than the public cloud and
the requests would otherwise silently target the wrong service.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from .base import SUPPORTED_PROVIDERS, AbstractSCMClient, RepoRef
from .errors import UnsupportedSCMProviderError

if TYPE_CHECKING:
    from ...config import Settings

logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, str] = {
    "github": "breezeai_cog.integrations.scm.github.GitHubSCMClient",
    "bitbucket": "breezeai_cog.integrations.scm.bitbucket.BitbucketSCMClient",
    "gitlab": "breezeai_cog.integrations.scm.gitlab.GitLabSCMClient",
    "azure_devops": "breezeai_cog.integrations.scm.azure_devops.AzureDevOpsSCMClient",
}
assert set(_PROVIDER_CLASSES) == SUPPORTED_PROVIDERS


def resolve_scm_token(provider: str, request_token: str | None, settings: Settings) -> str | None:
    """Request token, else the provider's global fallback, else ``None`` (anonymous)."""
    if request_token:
        return request_token
    secret = getattr(settings, f"scm_token_{provider}", None)
    if secret is not None:
        value: str = secret.get_secret_value()
        if value:
            logger.debug("Using the global %s token (no gitToken on the request).", provider)
            return value
    return None


def resolve_api_base_url(ref: RepoRef, settings: Settings) -> str | None:
    """Per-instance API base for a self-hosted ``ref``; ``None`` = provider default."""
    if not ref.is_self_hosted:
        return None
    mapped = settings.scm_instance_base_url_mapping.get(ref.host)
    if mapped:
        return mapped
    if ref.provider == "azure_devops" and ref.host.endswith(".visualstudio.com"):
        # Legacy per-organisation host; the public dev.azure.com/{org} API serves it.
        return None
    logger.warning(
        "Host %r is a self-hosted %s instance with no BREEZEAI_COG_SCM_INSTANCE_BASE_URL_MAPPING "
        "entry; falling back to the default %s API base URL. Map the host or API calls will "
        "target the wrong service.",
        ref.host, ref.provider, ref.provider,
    )
    return None


class SCMClientFactory:
    """Creates provider clients. ``for_repo`` is the entry point the server uses."""

    @staticmethod
    def create(
        provider: str,
        token: str | None,
        settings: Settings,
        api_base_url: str | None = None,
        **client_kwargs: Any,
    ) -> AbstractSCMClient:
        """Instantiate the client registered for ``provider``.

        Args:
            provider: Slug from ``SUPPORTED_PROVIDERS``.
            token: Credential, or ``None`` for anonymous access.
            settings: Application settings.
            api_base_url: REST base override for a self-hosted instance.
            **client_kwargs: Passed through to the client (``transport``, ``sleep``
                for tests).

        Raises:
            UnsupportedSCMProviderError: unknown slug (400 for the caller).
            SCMCredentialError: the provider rejected the credential's shape (400).
        """
        key = (provider or "").strip().lower()
        fqn = _PROVIDER_CLASSES.get(key)
        if not fqn:
            raise UnsupportedSCMProviderError(
                f"Unsupported git provider {provider!r}; expected one of "
                f"{sorted(SUPPORTED_PROVIDERS)}",
                provider=provider,
            )
        module_path, class_name = fqn.rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), class_name)
        logger.debug(
            "Creating SCM client provider=%s class=%s api_base_url=%s",
            key, class_name, api_base_url or "<default>",
        )
        client: AbstractSCMClient = cls(token, settings, api_base_url, **client_kwargs)
        return client

    @staticmethod
    def for_repo(
        ref: RepoRef,
        settings: Settings,
        request_token: str | None = None,
        **client_kwargs: Any,
    ) -> AbstractSCMClient:
        """Resolve credential and API base for ``ref`` and build its client."""
        return SCMClientFactory.create(
            ref.provider,
            resolve_scm_token(ref.provider, request_token, settings),
            settings,
            resolve_api_base_url(ref, settings),
            **client_kwargs,
        )


__all__ = ["SCMClientFactory", "resolve_api_base_url", "resolve_scm_token"]
