"""``framework: fetch`` on outbound calls made with the Fetch API.

The shared classifier records ``fetch(url)`` as an ``api_call`` by its name and leaves
``framework`` empty: a file may define its own ``fetch``. This detector labels the call only when
``fetch`` here is the Fetch API: called as ``fetch(…)`` / ``window.fetch(…)`` /
``globalThis.fetch(…)`` / ``self.fetch(…)``, and ``fetch`` is bound in this file by nothing, or
only by an import from a Fetch implementation (``node-fetch``, ``cross-fetch``, ``undici``, …).
A local function, variable, parameter or other import named ``fetch`` leaves it unlabelled.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..treesitter import node_text
from ..typescript.imports import _module_of
from ..typescript_sdk_common import label_http_client

#: Packages whose ``fetch`` is the Fetch API (a polyfill or a server implementation of it).
_FETCH_MODULES = frozenset({"node-fetch", "cross-fetch", "isomorphic-fetch", "isomorphic-unfetch",
                            "unfetch", "undici", "whatwg-fetch"})
_CALLEES = frozenset({"fetch", "window.fetch", "globalThis.fetch", "self.fetch"})


def fetch_is_the_api(root: Node, source: bytes) -> bool:
    """Whether ``fetch`` in this file can only be the Fetch API."""
    clear = True

    def names_fetch(node: Node | None) -> bool:
        return node is not None and node.type == "identifier" and node_text(node, source) == "fetch"

    def walk(n: Node) -> None:
        nonlocal clear
        if not clear:
            return
        if n.type == "import_statement":
            clause = next((c for c in n.named_children if c.type == "import_clause"), None)
            if clause is not None and b"fetch" in source[clause.start_byte:clause.end_byte] \
                    and _module_of(n, source) not in _FETCH_MODULES and _binds_fetch(clause):
                clear = False
            return
        if n.type in ("variable_declarator", "function_declaration", "class_declaration") \
                and names_fetch(n.child_by_field_name("name")):
            clear = False  # a class method named `fetch` is `this.fetch`, never a bare `fetch`
        elif n.type == "formal_parameters":
            for p in n.named_children:
                if names_fetch(p.child_by_field_name("pattern") or p.child_by_field_name("left") or p):
                    clear = False
        elif n.type == "arrow_function" and names_fetch(n.child_by_field_name("parameter")):
            clear = False
        for c in n.named_children:
            walk(c)

    def _binds_fetch(clause: Node) -> bool:
        stack = [clause]
        while stack:
            c = stack.pop()
            if c.type == "identifier" and node_text(c, source) == "fetch":
                return True
            stack.extend(c.named_children)
        return False

    walk(root)
    return clear


def fetch_evidence(root: Node, source: bytes) -> Callable[[Node], bool] | None:
    """Whether a call of this file is the global ``fetch``; None when the file rebinds it."""
    if not fetch_is_the_api(root, source):
        return None

    def evidenced(call: Node) -> bool:
        fn = call.child_by_field_name("function")
        return fn is not None and node_text(fn, source) in _CALLEES

    return evidenced


def _detect(dc: DetectContext) -> Detection | None:
    evidenced = fetch_evidence(dc.root, dc.source)
    if evidenced is None:
        return None
    emits = label_http_client(dc.root, dc.source, dc.path, dc.record, "fetch", evidenced)
    return Detection(emits) if emits else None


register_detector(Detector(
    name="fetch-calls", gate=Gate("typescript", order=37, guard=b"fetch"), detect=_detect,
    frameworks=("fetch",),
))
