"""``framework: axios`` on outbound calls made through axios.

The shared classifier (``detection/api_calls.py``) already records ``axios.get(url)`` as an
``api_call`` by its name; it leaves ``framework`` empty, since a name is no evidence. This
detector labels the call only when this file binds the client: a name imported from ``axios``
(``import axios from 'axios'``, ``import * as ax from 'axios'``, ``require('axios')``) or an
instance made from it (``const api = axios.create({…})``). A same-named parameter or local
anywhere in the file shadows that evidence, and nothing is labelled through it.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..outbound_helpers import RequestLabel, register_label
from ..treesitter import node_text
from ..typescript.imports import _module_of
from ..typescript_sdk_common import callee_root, label_http_client, shadowed_names

_MODULE = "axios"


def _require_of(value: Node | None, source: bytes) -> bool:
    """``require('axios')`` / ``require('axios').default``."""
    while value is not None and value.type == "member_expression":
        prop = value.child_by_field_name("property")
        if prop is None or node_text(prop, source) != "default":
            return False
        value = value.child_by_field_name("object")
    if value is None or value.type != "call_expression":
        return False
    fn, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return (fn is not None and node_text(fn, source) == "require" and first is not None
            and first.type == "string" and node_text(first, source)[1:-1] == _MODULE)


def axios_names(root: Node, source: bytes) -> frozenset[str]:
    """Names bound to axios in this file: its default or namespace import, a ``require``, and
    instances created from one of them with ``.create(…)``."""
    names: set[str] = set()
    for node in root.named_children:
        if node.type == "import_statement" and _module_of(node, source) == _MODULE:
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            for c in clause.named_children if clause is not None else []:
                if c.type == "identifier":
                    names.add(node_text(c, source))
                elif c.type == "namespace_import":
                    names.update(node_text(i, source) for i in c.named_children
                                 if i.type == "identifier")
    declarators: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            declarators.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    for d in declarators:
        name = d.child_by_field_name("name")
        if name is not None and name.type == "identifier" \
                and _require_of(d.child_by_field_name("value"), source):
            names.add(node_text(name, source))
    for d in declarators:  # `const api = axios.create({…})`
        name, value = d.child_by_field_name("name"), d.child_by_field_name("value")
        fn = value.child_by_field_name("function") \
            if value is not None and value.type == "call_expression" else None
        if name is not None and name.type == "identifier" and fn is not None \
                and fn.type == "member_expression":
            obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
            if obj is not None and prop is not None and node_text(prop, source) == "create" \
                    and node_text(obj, source) in names:
                names.add(node_text(name, source))
    return frozenset(names)


def axios_evidence(root: Node, source: bytes) -> Callable[[Node], bool] | None:
    """Whether a call of this file is made through axios; None when the file binds no axios."""
    clients = axios_names(root, source) - shadowed_names(root, source)
    if not clients:
        return None
    return lambda call: callee_root(call, source) in clients


def _detect(dc: DetectContext) -> Detection | None:
    evidenced = axios_evidence(dc.root, dc.source)
    if evidenced is None:
        return None
    emits = label_http_client(dc.root, dc.source, dc.path, dc.record, "axios", evidenced)
    return Detection(emits) if emits else None


register_detector(Detector(
    name="axios-calls", gate=Gate("typescript", order=36, guard=b"axios"), detect=_detect,
    frameworks=("axios",),
))

# The label an application's own helper built on this client carries to its call sites
# (``outbound_helpers``).
register_label(RequestLabel("axios", "typescript", (), axios_evidence))
