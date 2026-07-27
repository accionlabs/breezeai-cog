"""Groovy import extraction + FQCN resolution.

Groovy imports are fully-qualified class names (``com.acme.OrderRepo``), exactly like
Java, so this reuses the Java strategy: a repo-level **FQCN index** built by
``build_index`` maps each file's ``package.TypeName`` → repo-relative path. Wildcard
imports stay external; ``import a.b.Foo as Bar`` binds the alias.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import parallel_map, record_distinct
from ..treesitter import node_text, parse_source

_TYPE_DECLS = (
    "class_declaration", "interface_declaration", "enum_declaration", "trait_declaration",
)

#: "com.acme.Foo" → repo-relative path, or ``None`` when >1 file declares the same FQCN
#: (ambiguous → honest-null). Built by :func:`build_fqcn_index`.
FqcnIndex = dict[str, str | None]


def _has_decl_error(node: Node) -> bool:
    """A corrupt declaration header (direct-child ERROR/missing) — its name is fabricated,
    so it must not seed the FQCN index. Kept local so this picklable worker stays
    self-contained. Mirrors ``functions.has_declaration_error``."""
    return any(c.type == "ERROR" or c.is_missing for c in node.children)


def _package_of(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "package_declaration":
            nm = node.child_by_field_name("name")
            return node_text(nm, source) if nm is not None else ""
    return ""


def _type_name(node: Node, source: bytes) -> str | None:
    nm = node.child_by_field_name("name")
    return node_text(nm, source) if nm is not None else None


def _fqcn_index_one(args: tuple[str, str]) -> dict[str, str] | None:
    """Parse one Groovy file → ``{package.TypeName: rel}`` for its top-level types.
    Pure, picklable worker for :func:`parallel_map`; ``None`` on read/parse failure."""
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("groovy", source, 0).root_node
        package = _package_of(root, source)
        frag: dict[str, str] = {}
        for node in root.named_children:
            if node.type in _TYPE_DECLS and not _has_decl_error(node):
                name = _type_name(node, source)
                if name is not None:
                    frag[f"{package}.{name}" if package else name] = rel
        return frag
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="groovy",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files, jobs: int = 1) -> FqcnIndex:
    """Repo-level pre-pass: map each top-level type's fully-qualified name → repo path.
    A FQCN declared in >1 file collapses to ``None`` (ambiguous → honest-null). Parses
    each file across ``jobs`` processes."""
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    index: FqcnIndex = {}
    for frag in parallel_map(args, _fqcn_index_one, jobs):
        if frag:
            for fqcn, rel in frag.items():
                record_distinct(index, fqcn, rel)
    return index


def _resolve(fqcn: str, is_static: bool, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    hit = index.get(fqcn)  # None = absent or ambiguous → unresolved
    if hit is not None:
        return hit
    if is_static and "." in fqcn:  # static import: strip the member to get the class
        return index.get(fqcn.rsplit(".", 1)[0])
    return None


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path, index: FqcnIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}  # simple class/member name (or alias) → in-repo file

    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in node.children)
        is_wildcard = any(c.type == ".*" for c in node.children)
        name = node.child_by_field_name("name")
        if name is None:
            continue
        fqcn = node_text(name, source)
        if is_wildcard:
            external.setdefault(fqcn + ".*", None)
            continue
        resolved = _resolve(fqcn, is_static, index)
        (internal if resolved else external).setdefault(resolved or fqcn, None)
        if resolved:  # bind the alias if present, else the last FQCN segment
            alias = node.child_by_field_name("alias")
            key = node_text(alias, source) if alias is not None else fqcn.rsplit(".", 1)[-1]
            bindings[key] = resolved

    return list(internal), list(external), [], bindings  # Groovy has no explicit exports
