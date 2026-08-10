"""Groovy class / interface / enum / trait extraction → Class + flat methods + statements.

Groovy's heritage differs from Java's grammar shape: a ``superclass`` and a
``super_interfaces`` node (both node types, not fields) listing ``type_identifier`` /
``qualified_type`` types, and modifiers are inline children of the declaration (no
``modifiers`` wrapper).

A **trait** is emitted with its own ``trait`` class type (a member of the ``ClassType`` enum).
A class *uses* a trait via ``implements`` (not ``extends``), so the ``IMPLEMENTS`` edge the
agent traverses points at the ``trait``-typed node, and the trait's methods/heritage are all
still captured."""

from __future__ import annotations

from typing import Any

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
    "trait_declaration": "trait",
}
_NESTED_CLASS_TYPES = tuple(_TYPE)  # member (inner) types nested in a class body
_TYPE_NODES = ("type_identifier", "scoped_type_identifier", "generic_type", "qualified_type")
_VISIBILITY = ("public", "private", "protected")

# Enum-constant VALUE + doc capture (BREEZEAI-943). Mirrors ``java/classes.py`` — the
# dekobon Groovy grammar uses different comment / literal node-type names. (If a future
# language needs this too, factor the shared comment-association loop into
# ``parsers/comments.py`` per the note in the ticket.)
_COMMENT_TYPES = ("line_comment", "block_comment", "groovydoc_comment")
_LITERAL_TYPES = ("string_literal", "number_literal", "boolean_literal", "null_literal")


def _strip_comment(text: str) -> str:
    """A ``//`` / ``/* */`` / groovydoc ``/** */`` comment → its bare text (markers and
    per-line leading ``*`` removed)."""
    t = text.strip()
    if t.startswith("//"):
        return t[2:].strip()
    if t.startswith("/*"):
        t = t[2:]
        if t.startswith("*"):  # groovydoc /**
            t = t[1:]
        if t.endswith("*/"):
            t = t[:-2]
        lines = [ln.strip().lstrip("*").strip() for ln in t.splitlines()]
        return " ".join(ln for ln in lines if ln).strip()
    return t


def _enum_constant_value(constant: Node, source: bytes) -> str | None:
    """First **literal** argument of an ``enum_constant`` → its value (``HIGH("3")`` →
    ``3``). ``None`` when there is no argument or the first argument is not a compile-time
    literal — honest-null, never a guessed value."""
    args = constant.child_by_field_name("arguments")
    if args is None:
        return None
    first = next(iter(args.named_children), None)
    if first is None or first.type not in _LITERAL_TYPES:
        return None
    if first.type == "string_literal":
        frag = next((c for c in first.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else node_text(first, source).strip("'\"")
    return node_text(first, source)


def _enum_constants(body: Node, source: bytes) -> list[dict[str, str | None]]:
    """Enum members of an ``enum_body`` → ``[{name, value, doc}]`` in declaration order.
    ``doc`` is the associated comment — a trailing same-line comment if present, else the
    immediately-preceding comment (groovydoc or ``//``); ``None`` if neither."""
    kids = list(body.children)
    # First pass: each constant's trailing same-line comment. Record which comment nodes are
    # used so the leading-comment fallback can't reuse a previous constant's trailing comment
    # (which sits just before the next constant in the child list).
    trailing: dict[int, Node] = {}
    used: set[int] = set()
    for i, ch in enumerate(kids):
        if ch.type != "enum_constant":
            continue
        for nxt in kids[i + 1:]:
            if nxt.start_point[0] != ch.end_point[0]:
                break
            if nxt.type in _COMMENT_TYPES:
                trailing[i] = nxt
                used.add(nxt.start_byte)
                break
    out: list[dict[str, str | None]] = []
    for i, ch in enumerate(kids):
        if ch.type != "enum_constant":
            continue
        name_node = ch.child_by_field_name("name")
        if name_node is None:
            continue
        doc: str | None = None
        if i in trailing:  # trailing comment on the same line wins
            doc = _strip_comment(node_text(trailing[i], source))
        elif i > 0 and kids[i - 1].type in _COMMENT_TYPES and kids[i - 1].start_byte not in used:
            doc = _strip_comment(node_text(kids[i - 1], source))  # else the immediately-preceding comment
        out.append({
            "name": node_text(name_node, source),
            "value": _enum_constant_value(ch, source),
            "doc": doc,
        })
    return out


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

    metadata: dict[str, Any] | None = None
    body = node.child_by_field_name("body")
    if body is not None:
        if node.type == "enum_declaration":
            # Enum members are vocabulary, not behaviour — carry them (name/value/doc) on the
            # enum Class node as metadata rather than emitting statements. ``NAME("value")``
            # (constructor syntax) is otherwise captured nowhere.
            constants = _enum_constants(body, source)
            if constants:
                metadata = {"constants": constants}
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
        metadata=metadata,
    )
    return [cls, *nested_classes], methods, statements
