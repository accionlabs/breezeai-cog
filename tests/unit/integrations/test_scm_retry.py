"""`integrations/scm/retry.py`: which failures are retried, how long we wait, and what
gets logged."""

from __future__ import annotations

import logging

import httpx
import pytest

from breezeai_cog.integrations.scm.retry import (
    MAX_DELAY_SECONDS,
    RETRYABLE_STATUSES,
    request_with_retry,
    retry_delay,
)


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.example/x")
    resp = httpx.Response(status, request=req, headers=headers)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


class _Flaky:
    def __init__(self, failures: list[Exception], result: str = "ok") -> None:
        self.failures = list(failures)
        self.result = result
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


def _run(send, max_retries: int = 3):
    slept: list[float] = []
    out = request_with_retry(
        send, provider="Test", operation="GET /x", max_retries=max_retries,
        backoff_seconds=1.0, sleep=slept.append,
    )
    return out, slept


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retryable_status_is_retried_then_succeeds(status: int) -> None:
    send = _Flaky([_status_error(status), _status_error(status)])
    out, slept = _run(send)
    assert out == "ok" and send.calls == 3 and len(slept) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
def test_non_retryable_status_raises_immediately(status: int) -> None:
    send = _Flaky([_status_error(status)])
    with pytest.raises(httpx.HTTPStatusError):
        _run(send)
    assert send.calls == 1


def test_budget_exhausted_reraises_and_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    send = _Flaky([_status_error(503)] * 5)
    with caplog.at_level(logging.WARNING, logger="breezeai_cog.integrations.scm.retry"):
        with pytest.raises(httpx.HTTPStatusError):
            _run(send, max_retries=2)
    assert send.calls == 3
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING, logging.ERROR]


def test_zero_retries_means_one_attempt() -> None:
    send = _Flaky([_status_error(503)])
    with pytest.raises(httpx.HTTPStatusError):
        _run(send, max_retries=0)
    assert send.calls == 1


def test_transport_errors_are_retried() -> None:
    req = httpx.Request("GET", "https://api.example/x")
    send = _Flaky([httpx.ConnectTimeout("t", request=req), httpx.ReadTimeout("t", request=req)])
    out, slept = _run(send)
    assert out == "ok" and send.calls == 3 and len(slept) == 2


def test_retry_after_header_raises_the_wait() -> None:
    send = _Flaky([_status_error(429, {"Retry-After": "7"})])
    _, slept = _run(send)
    assert slept == [7.0]


def test_retry_after_is_capped() -> None:
    send = _Flaky([_status_error(429, {"Retry-After": "9999"})])
    _, slept = _run(send)
    assert slept == [MAX_DELAY_SECONDS]


def test_retry_delay_is_exponential_jittered_and_capped() -> None:
    assert retry_delay(2.0, 0, rng=lambda: 1.0) == 2.0
    assert retry_delay(2.0, 1, rng=lambda: 1.0) == 4.0
    assert retry_delay(2.0, 0, rng=lambda: 0.0) == 1.0  # jitter floor is 50 %
    assert retry_delay(2.0, 20, rng=lambda: 1.0) == MAX_DELAY_SECONDS
