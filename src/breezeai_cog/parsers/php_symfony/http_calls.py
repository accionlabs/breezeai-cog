"""Symfony HttpClient calls (``api_call``). The rules shared by every PHP HTTP-call detector
are in ``parsers/php_http_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php_http_common import Client, detect_http_calls


def _static(fqcn: str, method: str) -> str | None:
    if fqcn == "Symfony\\Component\\HttpClient\\HttpClient":
        return "symfony" if method in {"create", "createforbaseuri"} else None
    return None


_CLIENT = Client(
    types={
        "Symfony\\Contracts\\HttpClient\\HttpClientInterface": "symfony",
        "Symfony\\Component\\HttpClient\\CurlHttpClient": "symfony",
        "Symfony\\Component\\HttpClient\\NativeHttpClient": "symfony",
        "Symfony\\Component\\HttpClient\\ScopingHttpClient": "symfony",
    },
    named=frozenset(),
    generic=frozenset({"request"}),
    static=_static,
)
_MARKERS = (b'Symfony\\Contracts\\HttpClient', b'Symfony\\Component\\HttpClient')


def _detect(dc: DetectContext) -> Detection | None:
    if not any(marker in dc.source for marker in _MARKERS):
        return None
    return detect_http_calls(dc, _CLIENT)


register_detector(Detector(
    name="symfony-http-calls",
    gate=Gate("php", order=52),
    detect=_detect,
))
