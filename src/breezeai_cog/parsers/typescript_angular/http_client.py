"""``framework: angular`` on outbound calls made through Angular's ``HttpClient``.

The shared classifier records ``this.http.get(url)`` as an ``api_call`` by its name and leaves
``framework`` empty: ``http`` may be any object. This detector labels the call only when the
field is typed or injected as ``HttpClient`` imported from ``@angular/common/http`` in this file:
a constructor parameter property (``constructor(private http: HttpClient)``), a typed field
(``http: HttpClient``) or ``inject(HttpClient)``, called as ``this.<field>.<method>(…)``; or a
local bound once in the file to ``inject(HttpClient)`` or typed ``HttpClient``
(``const http = inject(HttpClient)``), called as ``http.<method>(…)``. The method must be one of
``HttpClient``'s request methods.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..treesitter import node_text
from ..typescript.imports import imported_locals
from ..typescript_sdk_common import (
    annotation_type, callee_root, label_http_client, shadowed_names, this_member_call,
)

_MODULE = "@angular/common/http"
#: ``HttpClient``'s request methods.
_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request",
                      "jsonp"})
_MODIFIERS = frozenset({"accessibility_modifier", "override_modifier", "readonly"})


def http_client_fields(root: Node, source: bytes, types: set[str]) -> frozenset[str]:
    """Fields of this file's classes that hold an ``HttpClient``. A name also declared as a
    field of another type in this file is left out."""
    found: dict[str, set[bool]] = {}

    def note(name: Node | None, is_client: bool) -> None:
        if name is not None:
            found.setdefault(node_text(name, source), set()).add(is_client)

    def walk(n: Node) -> None:
        if n.type == "required_parameter" and any(c.type in _MODIFIERS for c in n.children):
            note(n.child_by_field_name("pattern"), annotation_type(n, source) in types)
        elif n.type == "public_field_definition":
            value = n.child_by_field_name("value")
            injected = value is not None and value.type == "call_expression" \
                and node_text(value.child_by_field_name("function") or value, source) == "inject" \
                and _first_arg(value, source) in types
            note(n.child_by_field_name("name"), injected or annotation_type(n, source) in types)
        for c in n.named_children:
            walk(c)

    walk(root)
    return frozenset(name for name, kinds in found.items() if kinds == {True})


def http_client_locals(root: Node, source: bytes, types: set[str]) -> frozenset[str]:
    """Locals bound to an ``HttpClient``: ``const http = inject(HttpClient)`` or
    ``const http: HttpClient = …``. A name bound more than once in the file, or also a
    parameter, is left out."""
    found: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            injected = value is not None and value.type == "call_expression" \
                and node_text(value.child_by_field_name("function") or value, source) == "inject" \
                and _first_arg(value, source) in types
            if name is not None and name.type == "identifier" \
                    and (injected or annotation_type(n, source) in types):
                found.add(node_text(name, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return frozenset(found) - shadowed_names(root, source)


def _first_arg(call: Node, source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return node_text(first, source) if first is not None else None


def _detect(dc: DetectContext) -> Detection | None:
    types = imported_locals(dc.root, dc.source, _MODULE, frozenset({"HttpClient"}))
    fields = http_client_fields(dc.root, dc.source, types) if types else frozenset()
    locals_ = http_client_locals(dc.root, dc.source, types) if types else frozenset()
    if not fields and not locals_:
        return None

    def evidenced(call: Node) -> bool:
        member = this_member_call(call, dc.source)
        if member is not None:
            return member[0] in fields and member[1] in _METHODS
        fn = call.child_by_field_name("function")
        prop = fn.child_by_field_name("property") \
            if fn is not None and fn.type == "member_expression" else None
        return callee_root(call, dc.source) in locals_ and prop is not None \
            and node_text(prop, dc.source) in _METHODS

    emits = label_http_client(dc.root, dc.source, dc.path, dc.record, "angular", evidenced)
    return Detection(emits) if emits else None


register_detector(Detector(
    name="angular-http-client", gate=Gate("typescript", order=38, guard=b"@angular/common/http"),
    detect=_detect, frameworks=("angular",),
))
