"""Laravel HTTP client calls (``api_call``): the ``Http`` facade and ``PendingRequest``. The
rules shared by every PHP HTTP-call detector are in ``parsers/php_http_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php_http_common import VERBS, Client, detect_http_calls


def _static(fqcn: str, method: str) -> str | None:
    return "laravel" if fqcn == "Illuminate\\Support\\Facades\\Http" else None


_CLIENT = Client(
    types={
        "Illuminate\\Http\\Client\\PendingRequest": "laravel",
        "Illuminate\\Http\\Client\\Factory": "laravel",
    },
    named=VERBS - {"options"},
    generic=frozenset({"send"}),
    static=_static,
)
_MARKERS = (b'Illuminate\\Support\\Facades\\Http', b'Illuminate\\Http\\Client')


def _detect(dc: DetectContext) -> Detection | None:
    if not any(marker in dc.source for marker in _MARKERS):
        return None
    return detect_http_calls(dc, _CLIENT)


register_detector(Detector(
    name="laravel-http-calls",
    gate=Gate("php", order=50),
    detect=_detect,
))
