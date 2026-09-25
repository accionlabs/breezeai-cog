"""SCM-layer exceptions.

Each carries an HTTP ``status_code`` so the route can map it onto ``ApiError`` without
the integration package importing from ``server/``. Messages never include a provider
response body or a credential: bodies are logged at DEBUG by the HTTP layer, and clone
URLs are scrubbed by the orchestration before they reach any exception text.
"""

from __future__ import annotations


class SCMError(Exception):
    """Base class. ``status_code`` is what the HTTP route should answer with."""

    status_code: int = 502

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider: str = "",
        operation: str = "",
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.provider = provider
        self.operation = operation


class SCMAPIError(SCMError):
    """A provider REST call failed (after retries, when the failure was retryable).

    ``http_status`` is the provider's status; ``status_code`` stays 502 because the
    failure is upstream of this service.
    """

    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        provider: str = "",
        operation: str = "",
    ) -> None:
        super().__init__(message, provider=provider, operation=operation)
        self.http_status = http_status


class SCMCredentialError(SCMError):
    """The supplied credential is malformed for the provider (e.g. a Bitbucket token
    without the ``username:`` half). A caller error, so 400."""

    status_code = 400


class UnsupportedSCMProviderError(SCMError):
    """The repo URL names a host no provider claims. A caller error, so 400."""

    status_code = 400
