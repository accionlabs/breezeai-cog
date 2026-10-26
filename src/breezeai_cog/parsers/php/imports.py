"""PHP ``use`` and ``require`` extraction with repo-local path resolution."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..php_route_common import string_value, unwrap, walk
from ..treesitter import node_text
from .index import PhpIndex, namespace_of

_INCLUDE_TYPES = {
    "include_expression", "include_once_expression", "require_expression", "require_once_expression",
}


def _anchor_levels(node: Node, source: bytes) -> int | None:
    """How many folders above the including file's folder an anchor points:
    ``__DIR__`` / ``dirname(__FILE__)`` → 0, ``dirname(__DIR__)`` → 1,
    ``dirname(__FILE__, n)`` → n - 1, ``dirname(__DIR__, n)`` → n. None for anything else."""
    if node.type == "name" and node_text(node, source) == "__DIR__":
        return 0
    if node.type != "function_call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or node_text(function, source).lstrip("\\").lower() != "dirname":
        return None
    args = [unwrap(a) for a in (node.child_by_field_name("arguments") or node).named_children]
    if not args or args[0] is None or args[0].type != "name":
        return None
    magic = node_text(args[0], source)
    if len(args) == 1:
        levels = 1
    elif len(args) == 2 and args[1] is not None and args[1].type == "integer":
        levels = int(node_text(args[1], source))
    else:
        return None
    if magic == "__FILE__":
        return levels - 1 if levels >= 1 else None
    if magic == "__DIR__":
        return levels
    return None


def _anchored_include(node: Node, source: bytes, file_path: str, repo_root: Path) -> str | None:
    """The repo file an include/require loads, only when its path is anchored to this file
    (``__DIR__ . '/x.php'``, ``dirname(__FILE__) . '/x.php'``). A bare ``'x.php'`` resolves
    through ``include_path`` and the working directory at runtime, so it is not linked."""
    expr = unwrap(next(iter(node.named_children), None))
    if expr is None or expr.type != "binary_expression":
        return None
    operator = expr.child_by_field_name("operator")
    if operator is None or node_text(operator, source) != ".":
        return None
    left, right = unwrap(expr.child_by_field_name("left")), expr.child_by_field_name("right")
    levels = _anchor_levels(left, source) if left is not None else None
    tail = string_value(right, source)
    if levels is None or tail is None or not tail.startswith("/"):
        return None
    folder = (repo_root / file_path).parent
    for _ in range(levels):
        folder = folder.parent
    candidate = (folder / tail.lstrip("/")).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return repo_relative(candidate, repo_root) if candidate.is_file() else None


def _import_name(node: Node, source: bytes) -> tuple[str, str]:
    clause = node
    if clause.type != "namespace_use_clause":
        clause = next(
            (child for child in clause.named_children if child.type == "namespace_use_clause"),
            clause,
        )
    names = [
        child for child in clause.named_children
        if child.type != "namespace_aliasing_clause"
    ]
    name_node = names[0] if names else None
    name = node_text(name_node, source).lstrip("\\") if name_node is not None else ""
    alias_clause = next(
        (child for child in clause.named_children if child.type == "namespace_aliasing_clause"),
        None,
    )
    if alias_clause is not None and alias_clause.named_children:
        alias = node_text(alias_clause.named_children[-1], source)
    else:
        alias = node_text(names[-1], source) if len(names) > 1 else ""
    return name, alias or name.rsplit("\\", 1)[-1]


def _import_kind(node: Node, source: bytes) -> str | None:
    text = node_text(node, source).strip()
    if text.startswith("use "):
        text = text[4:]
    if text.startswith("function "):
        return "function"
    if text.startswith("const "):
        return "const"
    return None


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: Path,
    index: PhpIndex | None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}
    namespace = namespace_of(root, source)
    fqcn_index = index.fqcn if index is not None else {}
    folded_index = index.folded_fqcn if index is not None else {}

    for node in walk(root):
        if node.type == "namespace_use_declaration":
            default_kind = _import_kind(node, source)
            group = node.child_by_field_name("body")
            prefix_node = next(
                (c for c in node.named_children if c is not group and c.type == "namespace_name"),
                None,
            )
            if group is not None:
                prefix = node_text(prefix_node, source).rstrip("\\") if prefix_node is not None else ""
                clauses = list(group.named_children)
                imports = [
                    (
                        *_import_name(clause, source),
                        prefix,
                        _import_kind(clause, source) or default_kind,
                    )
                    for clause in clauses
                ]
            else:
                clauses = [
                    c for c in node.named_children if c.type == "namespace_use_clause"
                ]
                if not clauses:
                    continue
                imports = [
                    (*_import_name(clause, source), "", _import_kind(clause, source) or default_kind)
                    for clause in clauses
                ]

            for target, alias, base, kind in imports:
                fqcn = f"{base}\\{target}".strip("\\") if base else target.lstrip("\\")
                if kind is not None:
                    external.setdefault(f"{kind} {fqcn}", None)
                    continue
                resolved = fqcn_index.get(fqcn)
                if resolved is None:
                    resolved = folded_index.get(fqcn.casefold())
                (internal if resolved else external).setdefault(resolved or fqcn, None)
                if resolved:
                    bindings[alias] = resolved

        elif node.type in _INCLUDE_TYPES:
            rel = _anchored_include(node, source, file_path, repo_root)
            if rel is not None:
                internal.setdefault(rel, None)

    if namespace and index is not None:
        for simple, resolved in index.namespace_members.get(namespace, {}).items():
            if resolved:
                bindings.setdefault(simple, resolved)

    return list(internal), list(external), [], bindings
