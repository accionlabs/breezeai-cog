"""Shared synchronous HTTP plumbing for the provider clients.

One place for what every provider needs and none should re-implement: a pooled
``httpx.Client``, retry on transient failures, a pagination ceiling, and error
conversion that never puts a provider response body into an exception message.

Why the body stays out of the message: the old per-provider helpers raised
``RuntimeError(f"... {resp.status_code}: {resp.text}")``, and that text travelled into
the API error the caller saw and into logs. Provider bodies can echo request details,
and a 401 body from some hosts includes the token's own identifier. The body is logged
at DEBUG, truncated, for diagnosis.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from .errors import SCMAPIError
from .retry import request_with_retry

if TYPE_CHECKING:
    from ...config import Settings

logger = logging.getLogger(__name__)

#: How much of a failed response body to keep in the DEBUG log line.
_BODY_LOG_LIMIT = 500

#: Status-specific remediation appended to ``SCMAPIError`` messages. Kept short: the
#: caller is an operator reading an API error, not a person debugging the provider.
_HINTS: dict[int, str] = {
    401: "the credential was rejected; check the token is valid for this host",
    403: "the credential lacks access; check the token scope (GitLab needs read_api) "
    "and repository permissions",
    404: "repository or commit not found, or the token cannot see it",
    429: "rate limited by the provider",
}


class SCMHttpClient:
    """Thin ``httpx.Client`` wrapper with retry, pagination cap and error mapping.

    Args:
        provider: Human-readable provider name for log lines and error messages.
        base_url: API base; a ``path`` starting with ``/`` is appended to it, while a
            full ``http(s)://`` URL (e.g. a ``next`` cursor) is used as-is.
        headers: Sent on every request. Put the auth header here.
        settings: Supplies ``scm_api_timeout``, ``scm_api_retry_max``,
            ``scm_api_retry_backoff_seconds`` and ``scm_max_pages``.
        transport: Injectable for tests (``httpx.MockTransport``).
        sleep: Injectable for tests; the retry helper's wait.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        headers: Mapping[str, str],
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._timeout = settings.scm_api_timeout
        self._max_retries = settings.scm_api_retry_max
        self._backoff = settings.scm_api_retry_backoff_seconds
        self._max_pages = settings.scm_max_pages
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.Client | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def _session(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            kwargs: dict[str, Any] = {"timeout": self._timeout, "headers": self._headers}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    # ── Requests ───────────────────────────────────────────────────────────

    def url_for(self, path_or_url: str) -> str:
        """Absolute URL for ``path_or_url`` (a path is joined to ``base_url``)."""
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self.base_url}{path_or_url}"

    def get(
        self,
        path_or_url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET with retry. Returns the response; raises ``SCMAPIError`` on failure.

        Args:
            path_or_url: API path (appended to ``base_url``) or a full URL.
            params: Query parameters.
            headers: Extra headers for this request only (e.g. a different ``Accept``).
        """
        url = self.url_for(path_or_url)
        operation = f"GET {path_or_url.split('?', 1)[0]}"

        def _attempt() -> httpx.Response:
            resp = self._session().get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp

        try:
            return request_with_retry(
                _attempt,
                provider=self.provider,
                operation=operation,
                max_retries=self._max_retries,
                backoff_seconds=self._backoff,
                sleep=self._sleep,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.debug(
                "%s %s -> HTTP %d body=%r",
                self.provider, operation, status, exc.response.text[:_BODY_LOG_LIMIT],
            )
            hint = _HINTS.get(status)
            message = f"{self.provider} API {operation} failed with HTTP {status}"
            if hint:
                message += f": {hint}"
            raise SCMAPIError(
                message, http_status=status, provider=self.provider, operation=operation
            ) from None
        except httpx.TransportError as exc:
            raise SCMAPIError(
                f"{self.provider} API {operation} failed: {type(exc).__name__}",
                provider=self.provider, operation=operation,
            ) from None

    def get_json(
        self,
        path_or_url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET and decode JSON. A body that is not JSON is an ``SCMAPIError``."""
        resp = self.get(path_or_url, params, headers=headers)
        try:
            return resp.json()
        except ValueError:
            raise SCMAPIError(
                f"{self.provider} API GET {path_or_url} returned a non-JSON body",
                http_status=resp.status_code, provider=self.provider,
                operation=f"GET {path_or_url}",
            ) from None

    def get_text(
        self,
        path_or_url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """GET a raw file endpoint and return its body as UTF-8 text.

        Decoding is **strict**: a body that is not valid UTF-8 (a binary blob) raises
        ``SCMAPIError`` instead of coming back full of replacement characters. The
        orchestration treats a ``file_content`` failure as "skip this file", which is
        the right outcome for a binary — the old ``resp.text`` path wrote the mangled
        text into the temp tree and handed it to the parsers.
        """
        resp = self.get(path_or_url, params, headers=headers)
        return self.decode_utf8(resp.content, operation=f"GET {path_or_url}")

    def decode_utf8(self, data: bytes, *, operation: str) -> str:
        """Strictly decode ``data``; ``SCMAPIError`` when it is not UTF-8 text."""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SCMAPIError(
                f"{self.provider} API {operation} returned non-UTF-8 content (binary file)",
                provider=self.provider, operation=operation,
            ) from None

    def post_json(
        self,
        path_or_url: str,
        body: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST a JSON body and decode the JSON reply.

        **Not retried.** A POST that creates something (a PR comment) is not idempotent:
        a retry after an ambiguous failure could post twice. Failures map to
        ``SCMAPIError`` exactly like ``get``; a non-JSON 2xx body yields ``{}``.
        """
        url = self.url_for(path_or_url)
        operation = f"POST {path_or_url.split('?', 1)[0]}"
        try:
            resp = self._session().post(url, json=dict(body), params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.debug(
                "%s %s -> HTTP %d body=%r",
                self.provider, operation, status, exc.response.text[:_BODY_LOG_LIMIT],
            )
            hint = _HINTS.get(status)
            message = f"{self.provider} API {operation} failed with HTTP {status}"
            if hint:
                message += f": {hint}"
            raise SCMAPIError(
                message, http_status=status, provider=self.provider, operation=operation
            ) from None
        except httpx.TransportError as exc:
            raise SCMAPIError(
                f"{self.provider} API {operation} failed: {type(exc).__name__}",
                provider=self.provider, operation=operation,
            ) from None
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ── Pagination ─────────────────────────────────────────────────────────

    def paginate(
        self,
        first: str,
        next_of: Callable[[httpx.Response], str | None],
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[httpx.Response]:
        """Yield each page's response, following ``next_of`` until it returns ``None``.

        Bounded by ``scm_max_pages``. Reaching the cap logs a WARNING and stops, so a
        malformed or cyclic cursor, or a genuinely huge repository, cannot accumulate
        an unbounded result set in memory. The caller receives a truncated result and
        must treat the warning as the signal.

        Args:
            first: First page path or URL.
            next_of: Extracts the next page URL from a response (``Link`` header,
                ``X-Next-Page``, body ``next`` field …) or returns ``None``.
            params: Query parameters for the **first** request only. Providers that
                return a full ``next`` URL already embed them; providers that return a
                page number must build the URL inside ``next_of``.
        """
        url: str | None = first
        page_params = params
        pages = 0
        while url:
            pages += 1
            if pages > self._max_pages:
                logger.warning(
                    "breezeai_scm_pagination_capped: %s %s stopped after %d pages "
                    "(BREEZEAI_COG_SCM_MAX_PAGES); the result is truncated.",
                    self.provider, first.split("?", 1)[0], self._max_pages,
                )
                return
            resp = self.get(url, page_params)
            yield resp
            page_params = None
            url = next_of(resp)


def warn_if_anonymous(provider: str, token: str | None) -> None:
    """Log once per client when no credential was supplied.

    Anonymous access is allowed — public repositories work without a token and the
    existing ``/api/analyze-diff`` contract accepts a missing ``gitToken`` — but it is
    the first thing to check when a tree or compare call comes back 401/403/404.
    """
    if not token:
        logger.warning(
            "%s client created without a credential; only public repositories are "
            "reachable and API rate limits are the anonymous ones.",
            provider,
        )


__all__ = ["SCMHttpClient", "warn_if_anonymous"]
