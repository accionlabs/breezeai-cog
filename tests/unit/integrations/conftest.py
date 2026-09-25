"""Shared helpers for the provider client tests: a settings factory with retries made
instant, and a tiny routing mock transport."""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest

from breezeai_cog.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, scm_api_retry_backoff_seconds=0, scm_api_retry_max=1)


class Router:
    """Route requests by regex on the full URL; record every request seen.

    ``add(pattern, handler)`` — first matching pattern wins. Unmatched → 404 so a test
    that builds the wrong path fails loudly instead of hanging on a real network call.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[re.Pattern[str], Handler]] = []
        self.requests: list[httpx.Request] = []

    def add(self, pattern: str, handler: Handler | httpx.Response) -> "Router":
        h = handler if callable(handler) else (lambda _r, _resp=handler: _resp)
        self.routes.append((re.compile(pattern), h))
        return self

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        for pattern, handler in self.routes:
            if pattern.search(url):
                return handler(req)
        return httpx.Response(404, json={"message": f"no route for {url}"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def router() -> Router:
    return Router()
