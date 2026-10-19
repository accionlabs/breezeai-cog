"""PHP ``use`` and ``require`` extraction with repo-local path resolution."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text
from .index import PhpIndex


def _namespace(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "namespace_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                return node_text(name, source).lstrip("\\")
    return ""


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


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
    namespace = _namespace(root, source)
    fqcn_index = index.fqcn if index is not None else {}
    folded_index: dict[str, str | None] = {}
    for fqn, path in fqcn_index.items():
        key = fqn.casefold()
        if key in folded_index and folded_index[key] != path:
            folded_index[key] = None
        else:
            folded_index[key] = path

    for node in _walk(root):
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

        elif node.type == "include_expression":
            literal = next((c for c in node.named_children if c.type == "string"), None)
            if literal is None:
                continue
            raw = node_text(literal, source)
            if len(raw) < 2 or raw[0] not in {"'", '"'} or raw[-1] != raw[0]:
                continue
            relative = raw[1:-1]
            candidate = (repo_root / file_path).parent.joinpath(relative).resolve()
            try:
                candidate.relative_to(repo_root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                rel = repo_relative(candidate, repo_root)
                internal.setdefault(rel, None)

    if namespace:
        prefix = namespace + "\\"
        for fqn, resolved in fqcn_index.items():
            if resolved and fqn.startswith(prefix):
                suffix = fqn[len(prefix):]
                if "\\" not in suffix:
                    bindings.setdefault(suffix, resolved)

    return list(internal), list(external), [], bindings
