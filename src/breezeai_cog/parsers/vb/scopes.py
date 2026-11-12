"""What a VB name was declared as where it is used — the evidence SDK detectors key on.

::

    Private ReadOnly _s3 As IAmazonS3                 ' field
    Public Sub New(sqs As IAmazonSQS)                 ' parameter
        Dim gql = New GraphQLHttpClient(options, s)   ' local, typed by its New
        sqs.SendMessageAsync(…)        → IAmazonSQS (the parameter)
        gql.SendQueryAsync(…)          → GraphQLHttpClient (the local's New)
        Me._s3.PutObjectAsync(req)     → IAmazonS3 (the field, never a local)

A name is looked up where the call is: the enclosing lambda, then the enclosing member
(parameters and every declaration in its body), then each enclosing type's fields and
properties. Names are case-insensitive, as in VB. A name declared more than once in the scope
that has it binds only when every declaration agrees, and a declaration whose type can't be read
(an untyped lambda parameter, a ``For Each`` without ``As``, a misparsed form) makes the name
unknown — so a name is never bound past a declaration that shadows it.

The VB grammar misparses several declaration forms; the readable ones are handled here:
``Dim x As New T()`` (``New`` in an ``ERROR``, the type read as an array type),
``For Each x As T In …`` (the name in an ``ERROR``, the type in the ``variable`` field) and
``Using x As New T()`` / ``Using x = New T()``. ``Dim x As New T(args)`` splits into two
statements in the grammar and stays unknown.

This module also normalises calls (:func:`iter_calls`): a generic call
``client.SendQueryAsync(Of T)(request)`` parses as an element access on a call whose argument
list starts with ``Of`` (an ``ERROR``, or an ``Of Or…`` expression when ``T`` starts with an
operator keyword); it is read as the call it is, with ``request`` as its argument.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from tree_sitter import Node

from ..treesitter import node_text

#: Scopes whose declarations shadow the type's members, innermost first.
_MEMBERS = frozenset({
    "lambda_expression", "method_declaration", "constructor_declaration", "property_declaration",
    "event_declaration", "add_handler_block", "remove_handler_block", "raise_event_block",
})
_TYPES = frozenset({"class_block", "module_block", "structure_block", "interface_block"})
#: Node types that only wrap the one node that matters.
_WRAPPERS = frozenset({"expression", "argument", "literal", "statement"})


class Binding(NamedTuple):
    type: str | None  # the simple type name as written (no namespace, no ``(Of …)``), or unknown
    init: Node | None  # the ``New …`` the name was initialised with, if any


class Call(NamedTuple):
    node: Node  # the whole call (the element access for a generic call)
    receiver: Node  # what the method is called on
    method: str
    args: list[Node]  # the argument expressions, unwrapped


def bare(node: Node) -> Node:
    """``node`` without the grammar's single-child wrappers (``expression``, ``argument``, …)."""
    while node.type in _WRAPPERS and node.named_children:
        node = node.named_children[0]
    return node


def simple_type(text: str) -> str | None:
    """``Amazon.S3.IAmazonS3`` → ``IAmazonS3``; ``List(Of T)`` → ``List``; ``[Foo]`` → ``Foo``."""
    name = text.split("(", 1)[0].strip().rsplit(".", 1)[-1].strip("[] \t")
    return name or None


def _as_type(node: Node, source: bytes) -> str | None:
    """The type an ``As`` clause under ``node`` names (``As New T()`` included); None if none
    or unreadable."""
    clause = node if node.type == "as_clause" else next(
        (c for c in node.named_children if c.type == "as_clause"), None)
    if clause is None:
        return None
    typ = clause.child_by_field_name("type")
    if typ is None:
        return None
    inner = typ.named_children[0] if typ.named_children else typ
    if inner.type == "array_type":  # `As New T()`: the grammar reads `T()` as an array type
        if not any(c.type == "ERROR" and node_text(c, source) == "New" for c in clause.named_children):
            return None
        inner = next((c for c in inner.named_children if c.type == "namespace_name"), inner)
    name = simple_type(node_text(inner, source))
    return None if name is None or name.lower() == "new" else name


def _new_type(node: Node | None, source: bytes) -> tuple[str | None, Node | None]:
    if node is None:
        return None, None
    node = bare(node)
    if node.type != "new_expression":
        return None, None
    typ = node.child_by_field_name("type")
    return (simple_type(node_text(typ, source)) if typ is not None else None), node


def _declared(node: Node, source: bytes) -> Iterator[tuple[str, Binding]]:
    """``(lower-cased name, binding)`` for each name ``node`` declares."""
    t = node.type
    if t in ("parameter", "lambda_parameter"):
        name = node.child_by_field_name("name")
        if name is not None:
            yield node_text(name, source).lower(), Binding(_as_type(node, source), None)
    elif t in ("dim_statement", "variable_declarator"):
        names = [c for c in node.named_children if c.type == "identifier"
                 and node.field_name_for_child(node.children.index(c)) == "name"]
        declared = _as_type(node, source)
        new_t, init = _new_type(node.child_by_field_name("initializer"), source)
        for name in names:
            yield node_text(name, source).lower(), Binding(declared or new_t, init)
    elif t == "field_declaration":
        shared = _as_type(node, source)  # `Private a, b As T`
        for decl in node.named_children:
            if decl.type == "variable_declarator":
                for key, b in _declared(decl, source):
                    yield key, Binding(b.type or shared, b.init)
    elif t == "property_declaration":
        name = node.child_by_field_name("name")
        if name is not None:
            yield node_text(name, source).lower(), Binding(_as_type(node, source), None)
    elif t in ("for_statement", "for_each_statement"):
        var = node.child_by_field_name("variable")
        err = next((c for c in node.named_children if c.type == "ERROR"), None)
        if err is not None and var is not None and err.end_byte <= var.start_byte:
            # `For Each x As T In …`: the name in the ERROR, the type as the "variable"
            ident = next((c for c in err.named_children if c.type == "identifier"), None)
            if ident is not None:
                yield node_text(ident, source).lower(), Binding(
                    simple_type(node_text(var, source)), None)
        elif var is not None:
            yield node_text(var, source).lower(), Binding(_as_type(node, source), None)
    elif t == "catch_block":
        ex = node.child_by_field_name("exception")
        if ex is not None:
            yield node_text(ex, source).lower(), Binding(None, None)
    elif t == "using_statement":
        value = node.child_by_field_name("value")
        value = bare(value) if value is not None else None
        if value is None:
            return
        if value.type == "identifier":  # `Using u As New T()`: the As clause in an ERROR
            err = next((c for c in node.named_children if c.type == "ERROR"), None)
            yield node_text(value, source).lower(), Binding(
                _as_type(err, source) if err is not None else None, None)
        elif value.type == "binary_expression":  # `Using v = New T()`
            left = value.child_by_field_name("left")
            left = bare(left) if left is not None else None
            if left is not None and left.type == "identifier":
                new_t, init = _new_type(value.child_by_field_name("right"), source)
                yield node_text(left, source).lower(), Binding(new_t, init)


def _member_bindings(member: Node, source: bytes) -> dict[str, list[Binding]]:
    """Every name declared in a member: its parameters and every declaration in its body."""
    found: dict[str, list[Binding]] = {}
    stack = list(member.named_children)
    while stack:
        n = stack.pop()
        for name, b in _declared(n, source):
            found.setdefault(name, []).append(b)
        if n.type not in ("dim_statement", "field_declaration"):
            stack.extend(n.named_children)
    return found


def _type_bindings(block: Node, source: bytes) -> dict[str, list[Binding]]:
    """The fields and properties a type declares directly."""
    found: dict[str, list[Binding]] = {}
    for n in block.named_children:
        if n.type in ("field_declaration", "property_declaration"):
            for name, b in _declared(n, source):
                found.setdefault(name, []).append(b)
    return found


def _agreed(bindings: list[Binding]) -> Binding | None:
    types = {(b.type or "").lower() for b in bindings}
    if len(types) != 1 or "" in types:
        return None
    return bindings[0] if len(bindings) == 1 else Binding(bindings[0].type, None)


def lookup(at: Node, name: str, source: bytes, *, type_only: bool = False) -> Binding | None:
    """The binding ``name`` has where ``at`` is; None when it is undeclared, declared with an
    unreadable type, or declared differently in the scope that has it."""
    key = name.lower()
    node = at.parent
    while node is not None:
        scope: dict[str, list[Binding]] | None = None
        if node.type in _MEMBERS and not type_only:
            scope = _member_bindings(node, source)
        elif node.type in _TYPES:
            scope = _type_bindings(node, source)
        if scope is not None and key in scope:
            return _agreed(scope[key])
        node = node.parent
    return None


def receiver_type(receiver: Node, source: bytes) -> str | None:
    """The declared type of a call's receiver: a name (``_s3``), a member of ``Me`` / ``MyClass``
    (``Me._s3``), or an object created in place (``New TransferUtility(s3)``)."""
    receiver = bare(receiver)
    if receiver.type == "identifier":
        found = lookup(receiver, node_text(receiver, source), source)
        return found.type if found is not None else None
    if receiver.type == "member_access":
        obj, member = receiver.child_by_field_name("object"), receiver.child_by_field_name("member")
        if obj is not None and member is not None and \
                node_text(bare(obj), source).lower() in ("me", "myclass"):
            found = lookup(receiver, node_text(member, source), source, type_only=True)
            return found.type if found is not None else None
        return None
    return _new_type(receiver, source)[0]


def _args(arguments: Node | None) -> list[Node]:
    if arguments is None:
        return []
    return [bare(a) for a in arguments.named_children if a.type == "argument"]


def iter_calls(root: Node, source: bytes) -> Iterator[Call]:
    """Every ``x.Method(…)`` call under ``root``, generic calls (``x.Method(Of T)(…)``) read as
    one call."""
    consumed: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        if node.type == "element_access":
            inner = node.child_by_field_name("object")
            inner = bare(inner) if inner is not None else None
            if inner is None or inner.type != "invocation":
                continue
            arguments = inner.child_by_field_name("arguments")
            # `(Of T)` misparses as an ERROR holding `Of`, or — when T starts with an operator
            # keyword (`Of OrdersResponse`) — as an `Of Or …` expression: read the text.
            opening = node_text(arguments, source)[1:].lstrip().lower() if arguments is not None else ""
            if not (opening.startswith("of ") or opening.startswith("of\t")):
                continue
            target = inner.child_by_field_name("target")
            index = node.child_by_field_name("index")
            consumed.add(inner.id)
            if target is not None and target.type == "member_access":
                obj, member = target.child_by_field_name("object"), target.child_by_field_name("member")
                if obj is not None and member is not None:
                    yield Call(node, obj, node_text(member, source),
                               [bare(index)] if index is not None else [])
        elif node.type == "invocation" and node.id not in consumed:
            target = node.child_by_field_name("target")
            if target is None or target.type != "member_access":
                continue
            obj, member = target.child_by_field_name("object"), target.child_by_field_name("member")
            if obj is not None and member is not None:
                yield Call(node, obj, node_text(member, source),
                           _args(node.child_by_field_name("arguments")))


def local_creation(name: Node, source: bytes) -> Node | None:
    """The ``New …`` a local passed as an argument was initialised with, if declared once."""
    found = lookup(name, node_text(name, source), source)
    return found.init if found is not None else None


def initializer_pairs(creation: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """``(member, value)`` for every ``.Member = value`` in a ``With { … }`` initializer, nested
    ones included."""
    stack = [creation]
    while stack:
        n = stack.pop()
        if n.type == "member_initializer":
            member, value = n.child_by_field_name("member"), n.child_by_field_name("value")
            if member is not None and value is not None:
                yield node_text(member, source), bare(value)
        stack.extend(reversed(n.named_children))


def string_value(node: Node, source: bytes, depth: int = 0) -> str | None:
    """The value of a string expression built only from literals (``"a"``, ``"a" & "b"``), else
    None. VB escapes a quote as ``""`` and has no backslash escapes."""
    node = bare(node)
    if depth > 8:
        return None
    if node.type == "string_literal":
        text = node_text(node, source)
        return text[1:-1].replace('""', '"') if len(text) >= 2 and text[0] == '"' else None
    if node.type == "parenthesized_expression" and node.named_children:
        return string_value(node.named_children[0], source, depth + 1)
    if node.type == "binary_expression":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None:
            return None
        op = source[left.end_byte:right.start_byte].strip()
        if op not in (b"&", b"+"):
            return None
        a = string_value(left, source, depth + 1)
        b = string_value(right, source, depth + 1) if a is not None else None
        return a + b if a is not None and b is not None else None
    return None


def enclosing_member(node: Node) -> Node | None:
    """The method or constructor ``node`` is in."""
    up = node.parent
    while up is not None and up.type not in ("method_declaration", "constructor_declaration"):
        up = up.parent
    return up
