"""Guzzle HTTP client calls (``api_call``). The rules shared by every PHP HTTP-call detector
are in ``parsers/php_http_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php_http_common import VERBS, Client, detect_http_calls

_CLIENT = Client(
    types={"GuzzleHttp\\Client": "guzzle", "GuzzleHttp\\ClientInterface": "guzzle"},
    named=VERBS | {f"{verb}async" for verb in VERBS},
    generic=frozenset({"request", "requestasync"}),
)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_http_calls(dc, _CLIENT)


register_detector(Detector(
    name="guzzle-http-calls",
    gate=Gate("php", order=51, guard=b'GuzzleHttp\\'),
    detect=_detect,
))
