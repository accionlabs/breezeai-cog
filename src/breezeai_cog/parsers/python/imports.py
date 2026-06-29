"""Import extraction + in-repo resolution.

Internal imports (resolved to repo-relative ``.py`` / package ``__init__.py`` paths)
drive the IMPORTS edge; everything else is recorded as an external/library import.
``__all__`` (if present) becomes ``exports``.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text


def _dotted(node: Node, source: bytes) -> str:
    return node_text(node, source)


def _resolve(module: str, level: int, file_path: str, repo_root: Path) -> str | None:
    """Resolve a (possibly relative) module to an in-repo file path, or None."""
    abs_file = repo_root / file_path
    parts = [p for p in module.split(".") if p]
    if level > 0:
        base = abs_file.parent
        for _ in range(level - 1):
            base = base.parent
        target = base.joinpath(*parts) if parts else base
    else:
        target = repo_root.joinpath(*parts)
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return repo_relative(candidate, repo_root)
    return None


def _iter(node: Node):
    yield node
    for child in node.named_children:
        yield from _iter(child)


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path
) -> tuple[list[str], list[str], list[str]]:
    repo_root = Path(repo_root)
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    exports: list[str] = []

    for node in _iter(root):
        if node.type == "import_statement":
            for child in node.named_children:
                target = child.named_children[0] if child.type == "aliased_import" else child
                if target.type == "dotted_name":
                    module = _dotted(target, source)
                    resolved = _resolve(module, 0, file_path, repo_root)
                    (internal if resolved else external).setdefault(resolved or module, None)

        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name") or (
                node.named_children[0] if node.named_children else None
            )
            if module_node is None:
                continue
            level, module = 0, ""
            if module_node.type == "relative_import":
                for c in module_node.named_children:
                    if c.type == "import_prefix":
                        level = node_text(c, source).count(".")
                    elif c.type == "dotted_name":
                        module = _dotted(c, source)
            elif module_node.type == "dotted_name":
                module = _dotted(module_node, source)
            resolved = _resolve(module, level, file_path, repo_root)
            (internal if resolved else external).setdefault(resolved or module or ".", None)

        elif node.type == "assignment":
            target = node.named_children[0] if node.named_children else None
            if target is not None and target.type == "identifier" and node_text(target, source) == "__all__":
                for s in _iter(node):
                    if s.type == "string_content":
                        exports.append(node_text(s, source))

    return list(internal), list(external), exports
