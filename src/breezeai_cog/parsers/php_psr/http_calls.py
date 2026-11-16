"""PSR-18 HTTP client calls (``api_call``): ``sendRequest`` on a
``Psr\\Http\\Client\\ClientInterface``. The rules shared by every PHP HTTP-call detector are
in ``parsers/php_http_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..php_http_common import Client, detect_http_calls

_CLIENT = Client(
    types={"Psr\\Http\\Client\\ClientInterface": "psr"},
    named=frozenset(),
    generic=frozenset({"sendrequest"}),
    request_object=True,
)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_http_calls(dc, _CLIENT)


register_detector(Detector(
    name="psr-http-calls",
    gate=Gate("php", order=53, guard=b'Psr\\Http\\Client'),
    detect=_detect,
))
