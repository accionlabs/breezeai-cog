"""PHP import extraction and PSR-4 / FQCN resolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import parallel_map, record_distinct
from ..treesitter import node_text, parse_source

_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
)

#: "App\Models\User" -> repo-relative path, or None when ambiguous.
PhpFqcnIndex = dict[str, str | None]


@dataclass(frozen=True)
class PhpIndex:
    """Repo-level pre-pass result: FQCN -> path map for import resolution."""

    fqcn: PhpFqcnIndex = field(default_factory=dict)


def _php_index_one(args: tuple[str, str]) -> dict[str, str] | None:
    """Worker: parse one PHP file -> its `{Namespace\\TypeName: rel_path}` entries."""
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("php", source, 0).root_node
        namespace = ""
        for node in root.named_children:
            if node.type == "namespace_definition":
                nm = node.child_by_field_name("name")
                if nm is not None:
                    namespace = node_text(nm, source).strip("\\")
                break

        frag: dict[str, str] = {}
        for node in root.named_children:
            if node.type in _TYPE_DECLS:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    name = node_text(nm, source)
                    fqcn = f"{namespace}\\{name}" if namespace else name
                    frag[fqcn] = rel
        return frag
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        return None


def build_php_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PhpIndex:
    """Build the repo-wide FQCN index for PHP files."""
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    fqcn: PhpFqcnIndex = {}
    for frag in parallel_map(args, _php_index_one, jobs):
        if frag is None:
            continue
        for name, rel in frag.items():
            record_distinct(fqcn, name, rel)
    return PhpIndex(fqcn=fqcn)


def _seed_same_namespace(
    bindings: dict[str, str], namespace: str, index: dict[str, str | None] | None
) -> None:
    """Add same-namespace types to bindings so bare class calls in the same namespace resolve."""
    if not namespace or not index:
        return
    prefix = f"{namespace}\\"
    for fqcn, rel in index.items():
        if rel and fqcn.startswith(prefix):
            suffix = fqcn[len(prefix) :]
            if "\\" not in suffix:
                bindings.setdefault(suffix, rel)


def extract_imports(
    root: Node,
    source: bytes,
    path: str,
    repo_root: Path,
    index: PhpIndex | dict[str, str | None] | None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """Extract `use` imports from a PHP AST into internal, external, exports, bindings."""
    fqcn_map: dict[str, str | None] = index.fqcn if isinstance(index, PhpIndex) else (index or {})

    namespace = ""
    for child in root.named_children:
        if child.type == "namespace_definition":
            nm = child.child_by_field_name("name")
            if nm is not None:
                namespace = node_text(nm, source).strip("\\")
            break

    bindings: dict[str, str] = {}
    internal: list[str] = []
    external: list[str] = []

    # Process all namespace_use_declaration nodes
    for child in root.named_children:
        if child.type != "namespace_use_declaration":
            continue
        for clause in child.named_children:
            if clause.type != "namespace_use_clause":
                continue
            # Extract target FQCN
            target_node = next(
                (
                    c
                    for c in clause.named_children
                    if c.type in ("qualified_name", "name", "namespace_name")
                ),
                None,
            )
            if target_node is None:
                continue
            raw_target = node_text(target_node, source).strip("\\")

            # Extract alias or simple name
            alias_node = clause.child_by_field_name("alias")
            if alias_node is not None:
                simple_name = node_text(alias_node, source)
            else:
                simple_name = raw_target.rsplit("\\", 1)[-1]

            # In-repo PSR-4 resolution
            resolved_rel = fqcn_map.get(raw_target)
            if resolved_rel is not None:
                if resolved_rel not in internal:
                    internal.append(resolved_rel)
                bindings[simple_name] = resolved_rel
            else:
                if raw_target not in external:
                    external.append(raw_target)

    _seed_same_namespace(bindings, namespace, fqcn_map)

    exports: list[str] = []  # PHP has no JS-style export list
    return internal, external, exports, bindings
