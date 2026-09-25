"""Retry transient provider failures.

A provider that needs three attempts per call is the most common way an SCM
integration degrades, so every retry is logged at WARNING and budget exhaustion at
ERROR. Statuses 401/403/404/422 mean the request itself is wrong and are never
retried; repeating them only spends the budget a genuinely transient failure needs.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Final, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 429 is rate limiting; 502, 503 and 504 are a gateway, an overloaded backend and a
#: timeout. All transient by definition.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 502, 503, 504})

#: Upper bound on one wait, whatever the backoff or ``Retry-After`` says.
MAX_DELAY_SECONDS: Final[float] = 60.0


def retry_delay(backoff_seconds: float, attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter in ``[0.5, 1.0]`` of the nominal delay.

    Args:
        backoff_seconds: Base delay for the first retry.
        attempt: Zero-based index of the attempt that just failed.
        rng: Uniform ``[0, 1)`` source; injectable for tests.
    """
    nominal: float = min(backoff_seconds * (2**attempt), MAX_DELAY_SECONDS)
    return nominal * (0.5 + rng() * 0.5)


def _retry_after(exc: httpx.HTTPStatusError) -> float | None:
    """Seconds from a numeric ``Retry-After`` header, or ``None``."""
    raw = exc.response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; not worth parsing for a hint


def request_with_retry(
    send: Callable[[], T],
    *,
    provider: str,
    operation: str,
    max_retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``send``, retrying transient failures.

    Retries on a response status in ``RETRYABLE_STATUSES`` and on transport-level
    failures (connect errors, read timeouts), which are transient in the same sense.

    Args:
        send: Performs one attempt. Called afresh on each retry.
        provider: Provider name, for the log line.
        operation: What was requested, for the log line — typically ``GET <path>``.
        max_retries: Retries after the first attempt. Zero means one attempt.
        backoff_seconds: Base delay; the real wait is jittered and honours a numeric
            ``Retry-After`` when the provider sends one.
        sleep: Injectable for tests.

    Returns:
        Whatever ``send`` returned.

    Raises:
        httpx.HTTPStatusError: On a non-retryable status, or when the budget is spent.
        httpx.TransportError: When the budget is spent on transport failures.
    """
    for attempt in range(max_retries + 1):
        try:
            return send()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in RETRYABLE_STATUSES or attempt >= max_retries:
                if status in RETRYABLE_STATUSES:
                    logger.error(
                        "%s %s failed with HTTP %d and the retry budget is spent after %d "
                        "attempt(s); the cause is the provider, not the request.",
                        provider, operation, status, attempt + 1,
                    )
                raise
            delay = retry_delay(backoff_seconds, attempt)
            hinted = _retry_after(exc)
            if hinted is not None:
                delay = min(max(delay, hinted), MAX_DELAY_SECONDS)
            logger.warning(
                "%s %s failed with HTTP %d; retrying in %.2fs (attempt %d of %d).",
                provider, operation, status, delay, attempt + 1, max_retries,
            )
            sleep(delay)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                logger.error(
                    "%s %s failed at the transport level (%s) and the retry budget is "
                    "spent after %d attempt(s).",
                    provider, operation, type(exc).__name__, attempt + 1,
                )
                raise
            delay = retry_delay(backoff_seconds, attempt)
            logger.warning(
                "%s %s failed at the transport level (%s); retrying in %.2fs (attempt %d of %d).",
                provider, operation, type(exc).__name__, delay, attempt + 1, max_retries,
            )
            sleep(delay)

    # Unreachable: the loop returns or raises on its last iteration.
    raise RuntimeError(f"{provider} {operation} exhausted {max_retries} retries without a result.")


__all__ = ["MAX_DELAY_SECONDS", "RETRYABLE_STATUSES", "request_with_retry", "retry_delay"]
