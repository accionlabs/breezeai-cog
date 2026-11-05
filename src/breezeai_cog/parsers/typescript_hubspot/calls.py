"""HubSpot outbound calls: ``client.<resource>.<op>(...)`` on a ``Client`` from
``@hubspot/api-client`` is an ``api_call``, endpoint = the call chain
(``crm.objects.searchApi.doSearch``). See ``typescript_sdk_common.py``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..typescript_sdk_common import Sdk, detect_client_chain


# Verified against real hubspot-tools source. ``client_types`` are the SDK's client class as
# imported; ``operations`` are the SDK's read/write verbs seen on the client chain — kept to
# distinctive integration operations (not a bare ``get``) so a same-named method on an
# unrelated object cannot match by coincidence.
_HUBSPOT = Sdk(
    import_marker=b"@hubspot/api-client",
    framework="hubspot",
    client_types=frozenset({"Client"}),  # import { Client } from '@hubspot/api-client'
    # client.crm.objects.searchApi.doSearch / basicApi.getPage / batchApi.* / create / update / archive
    operations=frozenset(
        {
            "doSearch",
            "getPage",
            "getById",
            "create",
            "update",
            "archive",
            "createOrUpdate",
            "read",
        }
    ),
)


def _detect(dc: DetectContext) -> Detection | None:
    out: list[Emit] = []
    if not detect_client_chain(dc.root, dc.source, _HUBSPOT, out):
        return None
    return Detection(out, label=_HUBSPOT.framework)


register_detector(Detector(
    name="hubspot", gate=Gate("typescript", order=30, guard=b'@hubspot/api-client'), detect=_detect,
    frameworks=("hubspot",),
))
