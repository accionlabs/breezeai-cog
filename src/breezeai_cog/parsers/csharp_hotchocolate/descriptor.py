"""Fluent (descriptor-based) schema declaration.

HotChocolate has a second way to declare a schema: subclass ``ObjectType<T>`` and build the
fields in code rather than annotating a class.

    public class QueryType : ObjectType<Query> {
        protected override void Configure(IObjectTypeDescriptor<Query> d) {
            d.Field("books").Resolve(ctx => ...);      // an endpoint
        }
    }

The described type ``T`` plays exactly the role ``typeof(...)`` plays for ``[ExtendObjectType]``:
fields on a **root** are client-callable operations, fields on a data type are its shape. So only
a root-targeted descriptor yields routes — emitting every ``Field()`` call is the over-capture
the graphql-dotnet parser measured at 4,835 calls against ~520 real operations.

Unlike the attribute styles, these declarations live *inside a method body*, so this is the one
part of the parser that reads the tree rather than the finished ``FileRecord``.
"""

from __future__ import annotations

from typing import Iterator

from tree_sitter import Node

from ...schemas import Class, Function
from ..treesitter import node_text
from .mappings import (
    CONFIGURE_METHOD,
    DESCRIPTOR_BASES,
    DESCRIPTOR_PARAM_TYPES,
    FIELD_CALL,
    IGNORE_CALL,
    NAME_CALL,
)
from .naming import camel_case


def _generic_arg(type_text: str | None, bases: tuple[str, ...]) -> str | None:
    """``ObjectType<Query>`` → ``Query``, when the base name is one of ``bases``."""
    if not type_text or "<" not in type_text or not type_text.endswith(">"):
        return None
    base, _, inner = type_text.partition("<")
    if base.strip().rsplit(".", 1)[-1] not in bases:
        return None
    arg = inner[:-1].strip()
    return arg.rsplit(".", 1)[-1] or None if "," not in arg else None


def configure_method(record_functions: list[Function], cls: Class) -> Function | None:
    """The ``Configure`` override declared by ``cls``, if any."""
    for fn in record_functions:
        if fn.parentId == cls.id and fn.name == CONFIGURE_METHOD:
            return fn
    return None


def descriptor_target(cls: Class, configure: Function) -> str | None:
    """The type this descriptor describes, from the class's base type or, failing that, the
    ``Configure`` parameter type. Both are declarations of the same fact."""
    target = _generic_arg(cls.extends, DESCRIPTOR_BASES)
    if target is not None:
        return target
    for p in configure.params:
        arg = _generic_arg(p.type, DESCRIPTOR_PARAM_TYPES)
        if arg is not None:
            return arg
    return None


def _call_name(function_node: Node, source: bytes) -> str | None:
    """The invoked member's name — the trailing identifier of ``x.Foo`` or ``x.Foo<T>``."""
    if function_node.type != "member_access_expression":
        return None
    last = function_node.named_children[-1] if function_node.named_children else None
    if last is None:
        return None
    if last.type == "generic_name":
        inner = next((c for c in last.named_children if c.type == "identifier"), None)
        return node_text(inner, source) if inner is not None else None
    return node_text(last, source) if last.type == "identifier" else None


def _receiver_name(function_node: Node, source: bytes) -> str | None:
    """The object a member is accessed on — ``d`` in ``d.Field``."""
    first = function_node.named_children[0] if function_node.named_children else None
    return node_text(first, source) if first is not None and first.type == "identifier" else None


def _arguments(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    return [a.named_children[0] if a.type == "argument" and a.named_children else a
            for a in args.named_children]


def _string_arg(call: Node, source: bytes) -> str | None:
    for arg in _arguments(call):
        if arg is not None and arg.type == "string_literal":
            content = next((c for c in arg.named_children
                            if c.type == "string_literal_content"), None)
            return (node_text(content, source) if content is not None
                    else node_text(arg, source).strip('"'))
    return None


def _member_arg(call: Node, source: bytes) -> str | None:
    """The property named by ``Field(f => f.Title)`` → ``Title``."""
    for arg in _arguments(call):
        if arg is None or not arg.type.endswith("lambda_expression"):
            continue
        body = arg.child_by_field_name("body")
        if body is not None and body.type == "member_access_expression":
            last = body.named_children[-1] if body.named_children else None
            if last is not None and last.type == "identifier":
                return node_text(last, source)
    return None


def _chained_calls(field_call: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """``(name, invocation)`` for each builder call chained onto ``field_call``.

    ``d.Field("x").Resolve(…)`` nests the ``Field`` call *inside* the outer invocation, so the
    chain is walked upwards through ``member_access_expression`` parents.
    """
    node = field_call
    while True:
        access = node.parent
        if access is None or access.type != "member_access_expression":
            return
        invocation = access.parent
        if invocation is None or invocation.type != "invocation_expression":
            return
        name = _call_name(access, source)
        if name is not None:
            yield name, invocation
        node = invocation


def field_declarations(
    body: Node, source: bytes, receiver: str
) -> list[tuple[str, Node]]:
    """Yield ``(field_name, call_node)`` for each field declared on ``receiver`` in ``body``.

    Skips a field the chain explicitly ``Ignore()``s (it is removed from the schema), honours a
    chained ``Name("x")`` rename, and skips a declaration whose name cannot be read — the field
    exists but its name is not in front of us, and a fabricated one is worse than a gap.

    Returned in source order, so emitted statements follow the file rather than the walk.
    """
    found: list[tuple[str, Node]] = []
    stack = [body]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "invocation_expression":
            continue
        function_node = node.child_by_field_name("function")
        if function_node is None or _call_name(function_node, source) != FIELD_CALL:
            continue
        if _receiver_name(function_node, source) != receiver:
            continue
        chain = dict(_chained_calls(node, source))
        if IGNORE_CALL in chain:
            continue
        rename = chain.get(NAME_CALL)
        name = _string_arg(rename, source) if rename is not None else None
        if name is None:
            # A string argument is already the wire name; a property expression is a member, so
            # it takes the framework's camel-casing.
            member = _member_arg(node, source)
            name = _string_arg(node, source) or (camel_case(member) if member else None)
        if name:
            found.append((name, node))
    return sorted(found, key=lambda item: item[1].start_point)
