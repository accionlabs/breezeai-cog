"""C++ class / struct extraction → Class + flat member functions + statements.

A ``class_specifier`` / ``struct_specifier`` has a ``name`` (type_identifier), an
optional unnamed ``base_class_clause`` child (its ``type_identifier`` bases → the
heritage), and a ``body`` (field_declaration_list). Inside the body, a
``field_declaration`` whose declarator is a ``function_declarator`` is a member-function
declaration (emitted as a flat method ``Function``); one whose declarator is a plain
``field_identifier`` is a member variable (not a function — skipped).

A struct is a class with ``type="struct"``. Members default to ``public`` in a struct
and ``private`` in a class, updated by ``access_specifier`` labels in the body."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import (
    build_member_function,
    extract_params,
    function_declarator_of,
    has_declaration_error,
)

_CLASS_TYPES = ("class_specifier", "struct_specifier")
#: Class-body members that may be a function: a method with a return type is a
#: ``field_declaration``; a constructor/destructor (no return type) is a ``declaration``;
#: an inline definition (or ``= default``) is a ``function_definition``.
_MEMBER_FN_TYPES = ("field_declaration", "function_definition", "declaration")


def _base_name(base: Node, source: bytes) -> str | None:
    """The base-class name from a ``base_class_clause`` entry — a ``type_identifier``,
    the head of a ``template_type`` (``Base<T>`` → ``Base``), or a ``qualified_identifier``."""
    if base.type == "type_identifier":
        return node_text(base, source)
    if base.type == "template_type":
        ti = base.child_by_field_name("name") or next(
            (c for c in base.named_children if c.type == "type_identifier"), None
        )
        return node_text(ti, source) if ti is not None else None
    if base.type == "qualified_identifier":
        return node_text(base, source)
    return None


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """(extends, implements) from the ``base_class_clause``. C++ has multiple
    inheritance — the first base is ``extends``, the rest are ``implements``."""
    clause = next((c for c in node.named_children if c.type == "base_class_clause"), None)
    if clause is None:
        return None, []
    bases: list[str] = []
    for c in clause.named_children:
        name = _base_name(c, source)
        if name is not None:
            bases.append(name)
    if not bases:
        return None, []
    return bases[0], bases[1:]


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
) -> tuple[list[Class], list[Function], list[Statement]]:
    """Return (classes, methods, statements) — all flat, linked by parentId. The class
    list is this class plus any nested (member) classes/structs parented to it."""
    name_node = node.child_by_field_name("name")
    if name_node is None:  # anonymous struct/class — no name to emit; skip (honest gap)
        return [], [], []
    if node.child_by_field_name("body") is None:  # forward declaration (`class Foo;`) — not a
        return [], [], []                          # definition; emitting it fabricates a hollow node
    name = node_text(name_node, source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)
    is_struct = node.type == "struct_specifier"

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        # A class body holds only declarations (no executable statements), so member
        # functions are extracted directly — nothing goes through extract_statements here.
        visibility = "public" if is_struct else "private"
        for member in body.named_children:
            member = _unwrap_template(member)
            if has_declaration_error(member):
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if member.type == "access_specifier":
                visibility = node_text(member, source)
            elif member.type in _MEMBER_FN_TYPES:
                if function_declarator_of(member.child_by_field_name("declarator")) is None:
                    continue  # a member variable, not a function
                fn, fn_statements = build_member_function(
                    member, source, path,
                    parent_id=cid, class_name=name, visibility=visibility, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if fn.type == "constructor" and not ctor_params:
                    fd = function_declarator_of(member.child_by_field_name("declarator"))
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(
                            fd.child_by_field_name("parameters") if fd is not None else None, source
                        )
                    ]
            elif member.type in _CLASS_TYPES:
                sub_classes, sub_methods, sub_statements = build_class(
                    member, source, path,
                    parent_id=cid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_statements)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="struct" if is_struct else "class",
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements


def _unwrap_template(node: Node) -> Node:
    """A ``template_declaration`` wraps its class/struct/function — return the inner
    declaration so it is classified normally (else return the node unchanged)."""
    if node.type != "template_declaration":
        return node
    inner = next(
        (c for c in node.named_children
         if c.type in (*_CLASS_TYPES, "function_definition", "field_declaration")),
        None,
    )
    return inner if inner is not None else node
