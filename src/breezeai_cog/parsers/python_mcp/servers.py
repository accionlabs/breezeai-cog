"""Fact ``mcp.servers``: the module-level FastMCP server instances of each file.

A tools module often imports the server from where it is built (``from .server import mcp``)
and decorates with ``@mcp.tool``; the tools module itself never mentions ``FastMCP``. This
fact lets the detector prove such a receiver is a server, not an agent or another object
that happens to have a ``.tool`` decorator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text

FACT = "mcp.servers"

#: Markers of the Python MCP server SDKs.
SDK_MARKERS = (b"fastmcp", b"FastMCP", b"mcp.server")


#: Modules that export the FastMCP server class: the standalone package and the official SDK.
SDK_MODULES = ("fastmcp", "mcp.server.fastmcp")


def server_class_names(root: Node, source: bytes) -> set[str]:
    """What ``FastMCP`` is called in this file, counting only an import from an MCP SDK
    (``from fastmcp import FastMCP as F`` → ``F``; ``import fastmcp`` → ``fastmcp.FastMCP``).
    A class of the same name from anywhere else (a local shim) is not a server."""
    out: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None or node_text(module, source) not in SDK_MODULES:
                continue
            for child in node.named_children[1:]:
                if child.type == "dotted_name" and node_text(child, source) == "FastMCP":
                    out.add("FastMCP")
                elif child.type == "aliased_import":
                    name, alias = (
                        child.child_by_field_name("name"),
                        child.child_by_field_name("alias"),
                    )
                    if (
                        name is not None
                        and alias is not None
                        and node_text(name, source) == "FastMCP"
                    ):
                        out.add(node_text(alias, source))
        elif node.type == "import_statement":
            for child in node.named_children:
                target = (
                    child.child_by_field_name("name") if child.type == "aliased_import" else child
                )
                alias = (
                    child.child_by_field_name("alias") if child.type == "aliased_import" else None
                )
                if target is not None and node_text(target, source) in SDK_MODULES:
                    local = (
                        node_text(alias, source) if alias is not None else node_text(target, source)
                    )
                    out.add(f"{local}.FastMCP")
    return out


def is_server_call(node: Node | None, source: bytes, classes: set[str]) -> bool:
    """A call constructing the SDK server class (``FastMCP(…)`` / ``fastmcp.FastMCP(…)``)."""
    if node is None or node.type != "call":
        return False
    fn = node.child_by_field_name("function")
    return fn is not None and node_text(fn, source) in classes


def server_assignments(
    root: Node, source: bytes, classes: set[str], *, module_only: bool
) -> set[str]:
    """Names assigned a FastMCP server (``mcp = FastMCP("x")``), at module level only when
    ``module_only`` (what another file can import)."""
    out: set[str] = set()
    if not classes:
        return out
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "identifier"
                and is_server_call(right, source, classes)
            ):
                out.add(node_text(left, source))
        if module_only and node.type in ("function_definition", "class_definition"):
            continue
        stack.extend(node.named_children)
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> Any:
    names = server_assignments(root, source, server_class_names(root, source), module_only=True)
    return (rel, tuple(sorted(names))) if names else None


def _resolve(values: list[Any], index: object) -> dict[str, frozenset[str]] | None:
    out = {rel: frozenset(names) for rel, names in values}
    return out or None


def mcp_servers(index: Any | None) -> dict[str, frozenset[str]]:
    fact: dict[str, frozenset[str]] | None = index_fact(index, FACT)
    return fact or {}


register_fact(
    FactSpec(name=FACT, language="python", gate=(b"FastMCP",), collect=_collect, resolve=_resolve)
)
