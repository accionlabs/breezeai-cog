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

from collections.abc import Callable
from typing import Any

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ClassType, ConstructorParam, Function, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .functions import (
    build_member_function,
    extract_params,
    function_declarator_of,
    has_declaration_error,
)

#: Aggregate specifiers that become a ``Class`` node, mapped to their ``type``. A union is
#: a distinct kind (overlapping storage, one member active at a time) — it is not a struct,
#: so it carries its own type rather than being flattened to ``struct``.
_AGGREGATE_TYPE: dict[str, ClassType] = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "union",
}
_CLASS_TYPES = tuple(_AGGREGATE_TYPE)
#: Type-definition specifiers that can be *nested* inside a class body.
_NESTED_TYPE_SPECS = (*_CLASS_TYPES, "enum_specifier")


def _nested_type_spec(member: Node) -> Node | None:
    """The specifier of a nested type defined in a class body, or ``None`` for a real member.
    A nested ``union Chunk { … };`` / ``enum Kind { … };`` parses as a ``field_declaration``
    whose ``type`` is the specifier (not as a bare specifier), so both forms are unwrapped
    here. A member variable (``Chunk* root;``) has a plain type and returns ``None``."""
    if member.type in _NESTED_TYPE_SPECS:
        return member
    if member.type == "field_declaration":
        tnode = member.child_by_field_name("type")
        if tnode is not None and tnode.type in _NESTED_TYPE_SPECS:
            return tnode
    return None
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


#: Literal value kinds whose text is a verifiable compile-time value. Anything else (a
#: computed expression, a symbol) has no literal value → honest-null.
_LITERALS = ("string_literal", "number_literal", "char_literal", "true", "false")


def _strip_cpp_comment(text: str) -> str:
    """A C/C++ comment → its bare text: ``//`` / ``///`` / ``//!<`` line and ``/* */`` /
    ``/** */`` / ``/*!< */`` block markers (and per-line leading ``*``) removed."""
    t = text.strip()
    if t.startswith("//"):
        t = t[2:]
        if t[:1] in ("/", "!"):  # /// or //!
            t = t[1:]
        if t[:1] == "<":  # ///< or //!<
            t = t[1:]
        return t.strip()
    if t.startswith("/*"):
        t = t[2:]
        if t[:1] in ("*", "!"):  # /** or /*!
            t = t[1:]
        if t[:1] == "<":  # /*!<
            t = t[1:]
        if t.endswith("*/"):
            t = t[:-2]
        lines = [ln.strip().lstrip("*").strip() for ln in t.splitlines()]
        return " ".join(ln for ln in lines if ln).strip()
    return t


def _render_literal(node: Node | None, source: bytes) -> str | None:
    """A literal node → its value string (``"hi"`` → ``hi``, ``3`` → ``3``); ``None`` for
    a non-literal (computed expression / symbol) — never a guessed value."""
    if node is None or node.type not in _LITERALS:
        return None
    if node.type == "string_literal":
        content = next((c for c in node.named_children if c.type == "string_content"), None)
        return node_text(content, source) if content is not None else node_text(node, source).strip('"')
    return node_text(node, source)


def _declarator_name(node: Node | None, source: bytes) -> str | None:
    """Innermost declared name, unwrapping pointer/reference/array/init declarators
    (``* kName`` → ``kName``)."""
    if node is None:
        return None
    if node.type in ("field_identifier", "identifier"):
        return node_text(node, source)
    inner = node.child_by_field_name("declarator") or next(
        (c for c in node.named_children
         if c.type in ("field_identifier", "identifier") or c.type.endswith("declarator")),
        None,
    )
    return _declarator_name(inner, source) if inner is not None else None


def _const_field(field: Node, source: bytes) -> dict[str, str | None] | None:
    """A ``field_declaration`` that is a named constant (``constexpr`` or ``static const``)
    with a literal initializer → ``{name, value}``; ``None`` otherwise. A plain member
    variable (no initializer / not const-qualified) is not a constant."""
    quals = {node_text(c, source) for c in field.children
             if c.type in ("type_qualifier", "storage_class_specifier")}
    if "constexpr" not in quals and not ({"static", "const"} <= quals):
        return None
    value = next((v for c in field.named_children if (v := _render_literal(c, source)) is not None), None)
    if value is None:  # also handle an init_declarator wrapping the value
        init = next((c for c in field.named_children if c.type == "init_declarator"), None)
        if init is not None:
            value = _render_literal(init.child_by_field_name("value"), source)
    if value is None:
        return None
    name = _declarator_name(field.child_by_field_name("declarator"), source)
    return {"name": name, "value": value} if name is not None else None


def _member_docs(kids: list[Node], source: bytes, is_target: Callable[[Node], bool]) -> dict[int, str | None]:
    """Map the index of each target sibling → its doc comment: a trailing comment on the
    same line if present, else the immediately-preceding comment (guarded so a bare member
    can't inherit the previous member's trailing comment)."""
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
            docs[i] = _strip_cpp_comment(node_text(trailing[i], source))
        elif i > 0 and kids[i - 1].type == "comment" and kids[i - 1].start_byte not in used:
            docs[i] = _strip_cpp_comment(node_text(kids[i - 1], source))
        else:
            docs[i] = None
    return docs


def _class_constants(body: Node, source: bytes) -> list[dict[str, str | None]]:
    """Named constants declared directly in a class/struct body → ``[{name, value, doc}]``
    in declaration order. C++ class fields are otherwise captured nowhere (they are not
    functions); a code list is vocabulary, so it rides on the Class node's metadata."""
    kids = list(body.children)
    docs = _member_docs(kids, source, lambda n: n.type == "field_declaration")
    out: list[dict[str, str | None]] = []
    for i, ch in enumerate(kids):
        if ch.type != "field_declaration":
            continue
        info = _const_field(ch, source)
        if info is None:
            continue
        info["doc"] = docs.get(i)
        out.append(info)
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
    is_public_default = node.type in ("struct_specifier", "union_specifier")  # both default public

    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []
    ctor_params: list[ConstructorParam] = []

    body = node.child_by_field_name("body")
    if body is not None:
        # A class body holds only declarations (no executable statements), so member
        # functions are extracted directly — nothing goes through extract_statements here.
        visibility = "public" if is_public_default else "private"
        for member in body.named_children:
            member = _unwrap_template(member)
            if has_declaration_error(member):
                continue  # corrupt declaration header — skip rather than emit fabricated data
            if member.type == "access_specifier":
                visibility = node_text(member, source)
                continue
            spec = _nested_type_spec(member)
            if spec is not None:  # a nested type definition (class/struct/union/enum)
                if spec.type == "enum_specifier":
                    enum_cls = build_enum(spec, source, path, parent_id=cid, seen_ids=seen_ids)
                    if enum_cls is not None:
                        nested_classes.append(enum_cls)
                else:
                    sub_classes, sub_methods, sub_statements = build_class(
                        spec, source, path,
                        parent_id=cid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                    )
                    nested_classes.extend(sub_classes)
                    methods.extend(sub_methods)
                    statements.extend(sub_statements)
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

    # Named constants (constexpr / static const) declared in the body → vocabulary on the
    # Class node. C++ class fields are otherwise captured nowhere (they are not functions).
    metadata: dict[str, Any] | None = None
    if body is not None:
        constants = _class_constants(body, source)
        if constants:
            metadata = {"constants": constants}

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type=_AGGREGATE_TYPE.get(node.type, "class"),
        extends=extends,
        implements=implements,
        constructorParams=ctor_params,
        startLine=start,
        endLine=end,
        metadata=metadata,
    )
    return [cls, *nested_classes], methods, statements


def _enum_constants(node: Node, source: bytes) -> list[dict[str, str | None]]:
    """The members of an ``enum_specifier`` → ``[{name, value, doc}]`` in declaration order.
    ``value`` is the literal initializer (``OK = 3`` → ``3``), honest-null when absent or
    computed; ``doc`` is the associated ``//!<`` / ``//`` / ``/* */`` comment."""
    body = next((c for c in node.named_children if c.type == "enumerator_list"), None)
    if body is None:
        return []
    kids = list(body.children)
    docs = _member_docs(kids, source, lambda n: n.type == "enumerator")
    out: list[dict[str, str | None]] = []
    for i, e in enumerate(kids):
        if e.type != "enumerator":
            continue
        nm = e.child_by_field_name("name") or next(
            (c for c in e.named_children if c.type == "identifier"), None)
        if nm is None:
            continue
        out.append({
            "name": node_text(nm, source),
            "value": _render_literal(e.child_by_field_name("value"), source),
            "doc": docs.get(i),
        })
    return out


def build_enum(
    node: Node, source: bytes, path: str, *, parent_id: str, seen_ids: set[str]
) -> Class | None:
    """An ``enum_specifier`` → a ``Class`` of type ``enum`` carrying its members
    (``name``/``value``/``doc``) in ``metadata.constants`` — the same vocabulary shape used
    for class constants and JVM enums. Anonymous enums and forward declarations (``enum
    Color;`` — no ``enumerator_list``) emit nothing (honest gap, no hollow node). ``enum
    class`` / ``enum struct`` are scoped enums, captured the same way (scoped-ness not stored)."""
    name_node = node.child_by_field_name("name")
    if name_node is None:  # anonymous enum — no name to emit
        return None
    if not any(c.type == "enumerator_list" for c in node.named_children):
        return None  # forward declaration, not a definition
    name = node_text(name_node, source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    return Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="enum",
        startLine=start,
        endLine=end,
        metadata={"constants": _enum_constants(node, source)},
    )


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
