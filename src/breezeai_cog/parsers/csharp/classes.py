"""C# class / interface / enum / struct / record extraction → Class + flat methods
+ statements."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import build_method, extract_attributes, extract_params, flags
from .statements import extract_statements

_TYPE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "struct_declaration": "struct",
    "record_declaration": "record",
}
_METHOD_MEMBERS = ("method_declaration", "constructor_declaration",
                   "destructor_declaration", "operator_declaration")
_NESTED_CLASS_TYPES = tuple(_TYPE)  # member (nested) types declared in a type body


def _heritage(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Split ``base_list`` into (extends, implements). C# lists a single base class
    first (if any) followed by interfaces; interface names conventionally start ``I``,
    which we use to disambiguate the first entry."""
    base = node.child_by_field_name("base_list") or next(
        (c for c in node.named_children if c.type == "base_list"), None)
    if base is None:
        return None, []
    names = [node_text(c, source) for c in base.named_children
             if c.type in ("identifier", "qualified_name", "generic_name")]
    if not names:
        return None, []
    first = names[0]
    short = first.rsplit(".", 1)[-1]
    if len(short) >= 2 and short[0] == "I" and short[1].isupper():
        return None, names  # all interfaces
    return first, names[1:]


_CS_LITERALS = ("integer_literal", "real_literal", "string_literal", "character_literal", "boolean_literal")


def _strip_cs_comment(text: str) -> str:
    """A C# comment → its bare text: ``//`` / ``///`` line and ``/* */`` block markers
    removed, and a single-line XML ``<summary>…</summary>`` wrapper unwrapped."""
    t = text.strip()
    if t.startswith("//"):
        t = t[2:]
        if t[:1] == "/":  # /// XML doc
            t = t[1:]
        t = t.strip()
    elif t.startswith("/*"):
        t = t[2:]
        if t[:1] == "*":  # /**
            t = t[1:]
        if t.endswith("*/"):
            t = t[:-2]
        t = " ".join(ln.strip().lstrip("*").strip() for ln in t.splitlines()).strip()
    if t.startswith("<summary>") and t.endswith("</summary>"):
        t = t[len("<summary>"):-len("</summary>")].strip()
    return t


def _render_literal(node: Node | None, source: bytes) -> str | None:
    """A literal enum value → its text (``3`` → ``3``); ``None`` for a computed expression
    (``1 << 2``, ``-1``) — honest-null, never a guessed value."""
    if node is None or node.type not in _CS_LITERALS:
        return None
    if node.type == "string_literal":
        return node_text(node, source).strip('@$"')
    return node_text(node, source)


def _member_docs(kids: list[Node], source: bytes, is_target: Callable[[Node], bool]) -> dict[int, str | None]:
    """Index of each target sibling → its doc comment: a trailing same-line comment if
    present, else the immediately-preceding comment (guarded so a bare member can't inherit
    the previous member's trailing comment)."""
    trailing: dict[int, Node] = {}
    used: set[int] = set()
    for i, ch in enumerate(kids):
        if not is_target(ch):
            continue
        for nxt in kids[i + 1:]:
            if nxt.start_point[0] != ch.end_point[0]:
                break
            if nxt.type == "comment":
                trailing[i] = nxt
                used.add(nxt.start_byte)
                break
    docs: dict[int, str | None] = {}
    for i, ch in enumerate(kids):
        if not is_target(ch):
            continue
        if i in trailing:
            docs[i] = _strip_cs_comment(node_text(trailing[i], source))
        elif i > 0 and kids[i - 1].type == "comment" and kids[i - 1].start_byte not in used:
            docs[i] = _strip_cs_comment(node_text(kids[i - 1], source))
        else:
            docs[i] = None
    return docs


def _enum_constants(body: Node, source: bytes) -> list[dict[str, str | None]]:
    """Members of an ``enum_member_declaration_list`` → ``[{name, value, doc}]`` in
    declaration order. ``value`` is the literal initializer (honest-null when absent or
    computed); ``doc`` is the associated ``//`` / ``///`` / ``/* */`` comment."""
    kids = list(body.children)
    docs = _member_docs(kids, source, lambda n: n.type == "enum_member_declaration")
    out: list[dict[str, str | None]] = []
    for i, m in enumerate(kids):
        if m.type != "enum_member_declaration":
            continue
        nm = m.child_by_field_name("name")
        if nm is None:
            continue
        out.append({
            "name": node_text(nm, source),
            "value": _render_literal(m.child_by_field_name("value"), source),
            "doc": docs.get(i),
        })
    return out


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
) -> tuple[Class, list[Function], list[Statement]]:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends, implements = _heritage(node, source)

    visibility, _ = flags(node, source)
    is_abstract = node.type == "interface_declaration" or any(
        c.type == "modifier" and node_text(c, source) == "abstract" for c in node.children)

    methods: list[Function] = []
    statements: list[Statement] = []
    ctor_params: list[ConstructorParam] = []

    # record positional parameters (``record Money(decimal Amount)``) → constructorParams
    param_list = node.child_by_field_name("parameters") or next(
        (c for c in node.named_children if c.type == "parameter_list"), None)
    if param_list is not None:
        ctor_params = [ConstructorParam(name=p.name, type=p.type)
                       for p in extract_params(param_list, source)]

    body = node.child_by_field_name("body")
    if body is not None:
        statements.extend(
            extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids)
        )
        for member in body.named_children:
            if member.type in _METHOD_MEMBERS:
                fns, fn_statements = build_method(
                    member, source, path,
                    parent_id=cid, class_name=name, seen_ids=seen_ids, capture=capture, limit=limit,
                    resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_statements)
                if member.type == "constructor_declaration" and not ctor_params:
                    ctor_params = [
                        ConstructorParam(name=p.name, type=p.type)
                        for p in extract_params(member.child_by_field_name("parameters"), source)
                    ]

    # Enum members are vocabulary → carry them (name/value/doc) on the enum Class node,
    # matching the JVM and C++ enum/constant capture. `enum_member_declaration` is otherwise
    # captured nowhere (it is not a field_declaration).
    metadata: dict[str, Any] | None = None
    if node.type == "enum_declaration" and body is not None:
        constants = _enum_constants(body, source)
        if constants:
            metadata = {"constants": constants}

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
        decorators=extract_attributes(node, source),
        startLine=start,
        endLine=end,
        metadata=metadata,
    )
    return cls, methods, statements
