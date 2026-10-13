"""PHP import extraction and PSR-4 / FQCN resolution."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...logging import get_logger
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
    except OSError as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped",
            path=file_s,
            language="php",
            error_type=type(exc).__name__,
            error=str(exc),
        )
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
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped",
            path=file_s,
            language="php",
            error_type=type(exc).__name__,
            error=str(exc),
        )
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


def _literal_include_path(node: Node | None, source: bytes) -> str | None:
    """Return a statically-known PHP include path, without evaluating variables."""
    if node is None:
        return None
    if node.type == "parenthesized_expression":
        inner = node.named_children[0] if node.named_children else None
        return _literal_include_path(inner, source)
    if node.type in ("string", "encapsed_string"):
        parts: list[str] = []
        for child in node.named_children:
            if child.type != "string_content":
                return None
            parts.append(node_text(child, source))
        return "".join(parts)
    return None


def _include_target(node: Node, source: bytes, current_dir: Path) -> tuple[Path, str] | None:
    """Resolve a literal include, including the common ``__DIR__ . '/file.php'`` form."""
    literal = _literal_include_path(node, source)
    if literal is not None:
        target = Path(literal)
        return (target if target.is_absolute() else current_dir / target), literal

    if node.type == "parenthesized_expression" and node.named_children:
        return _include_target(node.named_children[0], source, current_dir)
    if node.type != "binary_expression" or len(node.named_children) != 2:
        return None
    left, right = node.named_children
    if left.type == "name" and node_text(left, source) == "__DIR__":
        suffix = _literal_include_path(right, source)
        if suffix is not None:
            return current_dir / suffix.lstrip("/\\"), suffix
    return None


def _walk_nodes(node: Node) -> Iterator[Node]:
    """Yield every named descendant, including nested require/include expressions."""
    for child in node.named_children:
        yield child
        yield from _walk_nodes(child)


def extract_imports(
    root: Node,
    source: bytes,
    path: str,
    repo_root: Path,
    index: PhpIndex | dict[str, str | None] | None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """Extract PHP ``use``, ``require``, and ``include`` imports."""
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

    def add_fqcn(raw_target: str, alias_node: Node | None = None) -> None:
        simple_name = (
            node_text(alias_node, source)
            if alias_node is not None
            else raw_target.rsplit("\\", 1)[-1]
        )
        resolved_rel = fqcn_map.get(raw_target)
        if resolved_rel is not None:
            if resolved_rel not in internal:
                internal.append(resolved_rel)
            bindings[simple_name] = resolved_rel
        elif raw_target not in external:
            external.append(raw_target)

    def is_function_import(node: Node) -> bool:
        return any(child.type == "function" for child in node.children)

    # Process all namespace_use_declaration nodes
    for child in root.named_children:
        if child.type != "namespace_use_declaration":
            continue
        group = next((node for node in child.named_children if node.type == "namespace_use_group"), None)
        group_prefix_node = next(
            (node for node in child.named_children if node.type == "namespace_name"), None
        )
        if group is not None and group_prefix_node is not None:
            if is_function_import(child):
                continue
            group_prefix = node_text(group_prefix_node, source).strip("\\")
            body = group.child_by_field_name("body")
            for clause in (body.named_children if body is not None else group.named_children):
                if clause.type != "namespace_use_clause":
                    continue
                target_node = next(
                    (node for node in clause.named_children if node.type in ("qualified_name", "name")),
                    None,
                )
                if target_node is not None:
                    target = node_text(target_node, source).strip("\\")
                    add_fqcn(f"{group_prefix}\\{target}", clause.child_by_field_name("alias"))
            continue
        for clause in child.named_children:
            if clause.type != "namespace_use_clause":
                continue
            if is_function_import(clause):
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

            add_fqcn(raw_target, clause.child_by_field_name("alias"))

    current_dir = (repo_root / path).parent
    root_dir = repo_root.resolve()
    include_types = frozenset(
        {"require_expression", "require_once_expression", "include_expression", "include_once_expression"}
    )
    for node in _walk_nodes(root):
        if node.type not in include_types or not node.named_children:
            continue
        resolved = _include_target(node.named_children[0], source, current_dir)
        if resolved is None:
            continue
        target_path, target_text = resolved
        try:
            rel = repo_relative(target_path.resolve(), root_dir)
        except (OSError, ValueError):
            rel = None
        if rel is not None and not rel.startswith("../") and (root_dir / rel).is_file():
            if rel not in internal:
                internal.append(rel)
        elif target_text not in external:
            external.append(target_text)

    _seed_same_namespace(bindings, namespace, fqcn_map)

    exports: list[str] = []  # PHP has no JS-style export list
    return internal, external, exports, bindings
