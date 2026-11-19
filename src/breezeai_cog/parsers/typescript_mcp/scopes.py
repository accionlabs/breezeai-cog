"""Where a handler passed by name is declared, for the TypeScript MCP detectors.

A name used inside a function or a block is the top-level function of that name only when no
enclosing scope binds it first: a parameter (destructured included), a ``const`` / ``let`` /
``var``, a nested ``function`` or ``class`` declaration, a ``catch`` parameter or a ``for … of``
/ ``for … in`` / ``for (let …)`` variable. Any such binding means the handler is that local,
whose declaration is not read, so it has no line.
"""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text

FUNCTION_SCOPES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "generator_function",
    }
)
_PARAMETERS = ("required_parameter", "optional_parameter")
_DECLARED = (
    "function_declaration",
    "generator_function_declaration",
    "class_declaration",
    "abstract_class_declaration",
)
_VARIABLES = ("lexical_declaration", "variable_declaration")


def binds(pattern: Node | None, name: str, source: bytes) -> bool:
    """True when the binding pattern ``pattern`` (a name, a destructuring) declares ``name``."""
    if pattern is None:
        return False
    if pattern.type in ("identifier", "shorthand_property_identifier_pattern"):
        return node_text(pattern, source) == name
    if pattern.type in ("assignment_pattern", "object_assignment_pattern"):
        return binds(pattern.child_by_field_name("left"), name, source)
    if pattern.type == "pair_pattern":
        return binds(pattern.child_by_field_name("value"), name, source)
    if pattern.type in ("object_pattern", "array_pattern", "rest_pattern"):
        return any(binds(c, name, source) for c in pattern.named_children)
    return False


def _declares(stmt: Node, name: str, source: bytes) -> bool:
    """True when the statement ``stmt`` declares ``name`` in the block that holds it."""
    if stmt.type in _VARIABLES:
        return any(
            d.type == "variable_declarator" and binds(d.child_by_field_name("name"), name, source)
            for d in stmt.named_children
        )
    if stmt.type in _DECLARED:
        n = stmt.child_by_field_name("name")
        return n is not None and node_text(n, source) == name
    return False


def _params_bind(fn: Node, name: str, source: bytes) -> bool:
    single = fn.child_by_field_name("parameter")  # `x => …`
    if single is not None and binds(single, name, source):
        return True
    params = fn.child_by_field_name("parameters")
    for p in params.named_children if params is not None else ():
        pattern = p.child_by_field_name("pattern") if p.type in _PARAMETERS else p
        if binds(pattern, name, source):
            return True
    if fn.type in ("function_expression", "function", "generator_function"):
        own = fn.child_by_field_name("name")  # `function lookup() { … lookup … }`
        if own is not None and node_text(own, source) == name:
            return True
    return False


def _var_in_body(fn: Node, name: str, source: bytes) -> bool:
    """A ``var`` of ``name`` anywhere in ``fn``'s body outside nested functions (it is hoisted
    to the whole function)."""
    body = fn.child_by_field_name("body")
    stack = [body] if body is not None else []
    while stack:
        n = stack.pop()
        if n.type == "variable_declaration" and _declares(n, name, source):
            return True
        stack.extend(c for c in n.named_children if c.type not in FUNCTION_SCOPES)
    return False


def locally_bound(ident: Node, source: bytes) -> bool:
    """True when a scope enclosing ``ident`` (below the module) binds its name."""
    name = node_text(ident, source)
    node = ident.parent
    while node is not None and node.type != "program":
        if node.type in FUNCTION_SCOPES:
            if _params_bind(node, name, source) or _var_in_body(node, name, source):
                return True
        elif node.type in ("statement_block", "class_static_block"):
            if any(_declares(s, name, source) for s in node.named_children):
                return True
        elif node.type == "switch_body":
            for case in node.named_children:
                if any(_declares(s, name, source) for s in case.children_by_field_name("body")):
                    return True
        elif node.type == "for_in_statement":
            if binds(node.child_by_field_name("left"), name, source):
                return True
        elif node.type == "for_statement":
            init = node.child_by_field_name("initializer")
            if init is not None and _declares(init, name, source):
                return True
        elif node.type == "catch_clause":
            if binds(node.child_by_field_name("parameter"), name, source):
                return True
        node = node.parent
    return False


def function_line(ident: Node, source: bytes, functions: dict[str, int]) -> int | None:
    """The line of the top-level function ``ident`` names (``functions``: name → line), or None
    when an enclosing scope binds the name to something else."""
    line = functions.get(node_text(ident, source))
    if line is None or locally_bound(ident, source):
        return None
    return line
