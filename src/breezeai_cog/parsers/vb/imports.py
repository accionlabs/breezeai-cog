"""VB.NET import extraction.

Like C#, VB ``Imports`` names a **namespace** (or an alias), not a file — so every
import is recorded external and calls resolve same-file only (precision-first)."""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text


def extract_imports(
    root: Node, source: bytes
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    external: dict[str, None] = {}
    for child in root.named_children:
        if child.type == "imports_statement":
            name = next((c for c in child.named_children if c.type == "namespace_name"), None)
            if name is not None:
                external.setdefault(node_text(name, source), None)
    return [], list(external), [], {}
