"""Groovy class / interface / enum / trait extraction → Class + flat methods + statements.

Groovy's heritage differs from Java's grammar shape: a ``superclass`` and a
``super_interfaces`` node (both node types, not fields) listing ``type_identifier`` /
``qualified_type`` types, and modifiers are inline children of the declaration (no
``modifiers`` wrapper).

A **trait** is mapped to the ``interface`` class type. The schema's ``ClassType`` enum is
restricted (``class/interface/struct/record/enum/module`` — no ``trait``), so any other
value would be dropped at ingestion. ``interface`` is also the right model for the reading
agent: a class *uses* a trait via ``implements`` (not ``extends``), so the ``IMPLEMENTS``
edge the agent traverses points at an ``interface``-typed node — internally consistent, and
the trait's methods/heritage are all still captured."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import build_method, extract_annotations, extract_params, has_declaration_error
from .statements import extract_statements

_TYPE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "trait_declaration": "interface",  # no `trait` in the ClassType enum; a trait is used via `implements`
}
_NESTED_CLASS_TYPES = tuple(_TYPE)  # member (inner) types nested in a class body
_TYPE_NODES = ("type_identifier", "scoped_type_identifier", "generic_type", "qualified_type")
_VISIBILITY = ("public", "private", "protected")


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    # ``superclass`` and ``super_interfaces`` are node types (not fields) in this grammar.
    extends: str | None = None
    superclass = next((c for c in node.named_children if c.type == "superclass"), None)
    if superclass is not None:
        ti = next((c for c in superclass.named_children if c.type in _TYPE_NODES), None)
        extends = node_text(ti, source) if ti is not None else None
    implements: list[str] = []
    interfaces = next((c for c in node.named_children if c.type == "super_interfaces"), None)
    if interfaces is not None:
        implements = [node_text(c, source) for c in interfaces.named_children if c.type in _TYPE_NODES]
    return extends, implements


def _flags(node: Node) -> tuple[str, bool]:
    """(visibility, is_abstract) from inline modifier children. Groovy defaults to public;
    interfaces are implicitly abstract."""
    visibility = "public"
    is_abstract = node.type == "interface_declaration"
    for c in node.children:
        if c.type in _VISIBILITY:
            visibility = c.type
        elif c.type == "abstract":
            is_abstract = True
    return visibility, is_abstract


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
    """Return (classes, methods, statements) — all flat, linked by parentId. The
    class list is this class plus any nested (inner) member types, each parented to
    its enclosing class."""
    name = node_text(node.child_by_field_name("name"), source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)
    visibility, is_abstract = _flags(node)

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for member in body.named_children:
            if has_declaration_error(member):
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if member.type in ("method_declaration", "constructor_declaration"):
                fn, fn_statements = build_method(
                    member, source, path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                    resolve=resolve,
                )
                methods.append(fn)
                statements.extend(fn_statements)
                if member.type == "constructor_declaration":
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(member.child_by_field_name("parameters"), source)
                    ]
            elif member.type in _NESTED_CLASS_TYPES:
                # Member (inner) class / interface / enum / trait — its own Class parented
                # to this one (recursing for arbitrarily deep nesting).
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
        type=_TYPE.get(node.type, "class"),
        visibility=visibility,
        isAbstract=is_abstract,
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        decorators=extract_annotations(node, source),
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements
