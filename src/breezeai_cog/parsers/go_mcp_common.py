"""Shared by the Go MCP packages (``go_mcpgo`` for ``github.com/mark3labs/mcp-go``, ``go_mcp``
for the official SDK ``github.com/modelcontextprotocol/go-sdk``): the tree walk, Go string
literals and the local names an import path is bound to. Each SDK's construct readers stay in
its own package."""

from __future__ import annotations

from tree_sitter import Node

from .treesitter import node_text


def walk(node: Node) -> list[Node]:
    """Every node under ``node``, in source order (an explicit stack: no recursion limit)."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(n.named_children))
    return out


def go_string(node: Node | None, source: bytes) -> str | None:
    """The text of a Go string literal (interpreted or raw; escapes kept as written)."""
    if node is None or node.type not in ("interpreted_string_literal", "raw_string_literal"):
        return None
    return node_text(node, source)[1:-1]


def package_names(root: Node, source: bytes, package: str) -> set[str]:
    """Local names the import path ``package`` is bound to in this file: its import alias, else
    its last path element (``…/mcp`` → ``mcp``). A blank (``_``) or dot import binds no name."""
    out: set[str] = set()
    for spec in walk(root):
        if spec.type != "import_spec":
            continue
        if go_string(spec.child_by_field_name("path"), source) != package:
            continue
        alias = spec.child_by_field_name("name")
        name = node_text(alias, source) if alias is not None else package.rsplit("/", 1)[-1]
        if name not in ("_", "."):
            out.add(name)
    return out
