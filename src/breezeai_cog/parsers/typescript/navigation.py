"""Shared pieces of the outbound-navigation detectors.

A link or a router call moves the user to a page: the UI's outbound call, matched to a page
(``route`` / ``routeKind: page``) on ``endpoint``, as ``api_call`` is to ``route``. Each
framework package registers its own detector (which receivers and calls count, how a target is
written); this module holds what they share: the tree walk, reading a literal target, and the
record they emit.

The address is resolved or empty: a literal absolute path is the address; a relative path
depends on the current route, a variable or a named route is not an address — all empty
(contract §6, §8). A call that is its own statement enriches it; one nested in a larger
statement gets its own record.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ..emits import Emit
from ..treesitter import node_text


def walk(node: Node) -> Iterator[Node]:
    yield node
    for c in node.named_children:
        yield from walk(c)


def literal(node: Node, source: bytes) -> str | None:
    """A string literal's text, or a template string with no substitution; else ``None``."""
    if node.type == "string":
        return node_text(node, source)[1:-1]
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.named_children
    ):
        return node_text(node, source)[1:-1]
    return None


def absolute(path: str | None) -> str | None:
    return path if path is not None and path.startswith("/") else None


def first_arg(call: Node) -> Node | None:
    args = call.child_by_field_name("arguments")
    return args.named_children[0] if args is not None and args.named_children else None


def navigation(node: Node, framework: str, endpoint: str | None, *, enrich: bool) -> Emit:
    """The ``route`` / ``routeKind: navigation`` record for one link or router call."""
    return Emit(
        node, {"semanticType": "route", "routeKind": "navigation", "framework": framework,
               "endpoint": endpoint},
        enrich="own" if enrich else "never",
    )
