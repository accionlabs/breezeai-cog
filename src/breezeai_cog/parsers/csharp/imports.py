"""C# import extraction.

C# ``using`` directives name **namespaces** (``System.Text``,
``Microsoft.AspNetCore.Mvc``), not files — a namespace spans many files across
assemblies, so there is no reliable 1:1 namespace→repo-path mapping. We therefore
record every ``using`` as an external import and resolve calls same-file only
(precision-first, Phase 1). ``global using`` / ``using static`` / ``using X = …``
alias forms are all captured by their namespace text.
"""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text

_NAME_NODES = ("qualified_name", "identifier", "alias_qualified_name", "member_access_expression")


def _namespace_of(node: Node, source: bytes) -> str | None:
    """The namespace named by a ``using_directive`` (last name node — after any alias)."""
    name: Node | None = None
    for child in node.named_children:
        if child.type in _NAME_NODES:
            name = child
    return node_text(name, source) if name is not None else None


def extract_imports(
    root: Node, source: bytes
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    external: dict[str, None] = {}

    def walk(node: Node) -> None:
        for child in node.named_children:
            if child.type == "using_directive":
                ns = _namespace_of(child, source)
                if ns:
                    external.setdefault(ns, None)
            elif child.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                walk(child)  # nested usings

    walk(root)
    # C# has no explicit exports; no in-repo import resolution (empty internal/bindings).
    return [], list(external), [], {}
