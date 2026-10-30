"""``$resource`` — ngResource's REST client — is an outbound ``api_call``::

    return $resource('/api/products/:id', { id: '@id' }, { update: { method: 'PUT' } });

One ``$resource`` declares up to several verbs (``get``, ``save``, ``query``, ``remove`` and the
custom ``actions``) at one place, and which of them the app calls is only visible at the calls.
So the record carries the ``endpoint`` and **no** ``method``; the shared HTTP-client classifier
is left alone because it would default a bare call to ``GET``."""

from __future__ import annotations

from ..emits import Emit
from ..treesitter import entry_text
from ..typescript.navigation import literal
from .syntax import FRAMEWORK, Scan


def resource_emits(found: Scan, source: bytes) -> list[Emit]:
    if "$resource" in found.declared:  # a function of the file's own, not the injected service
        return []
    emits: list[Emit] = []
    for call in found.resources:
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        emits.append(
            Emit(
                call,
                {
                    "semanticType": "api_call",
                    "framework": FRAMEWORK,
                    "endpoint": (literal(first, source) if first is not None else None) or None,
                },
                text=entry_text(call, source, nested=()),
            )
        )
    return emits
