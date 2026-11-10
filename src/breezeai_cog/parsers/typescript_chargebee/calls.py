"""Chargebee outbound calls: ``client.<resource>.<op>(...)`` on a ``Chargebee`` client is an
``api_call``, endpoint = the call chain (``customer.list``). See ``typescript_sdk_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..typescript_sdk_common import Sdk, detect_client_chain

_CHARGEBEE = Sdk(
    import_marker=b"chargebee",
    framework="chargebee",
    client_types=frozenset({"Chargebee"}),  # import Chargebee from 'chargebee'
    # client.customer.list / subscription.list / item.list / *.retrieve / *.create / *.update
    operations=frozenset({"list", "retrieve", "create", "update", "delete"}),
)


def _detect(dc: DetectContext) -> Detection | None:
    out: list[Emit] = []
    if not detect_client_chain(dc.root, dc.source, _CHARGEBEE, out):
        return None
    return Detection(out, label=_CHARGEBEE.framework)


register_detector(Detector(
    name="chargebee", gate=Gate("typescript", order=31, guard=b'chargebee'), detect=_detect,
    frameworks=("chargebee",),
))
