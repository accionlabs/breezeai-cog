"""Java import extraction + FQCN resolution.

Java imports are fully-qualified class names (``com.acme.OrderRepo``). They resolve
to in-repo files via a repo-level **FQCN index** built by ``build_index`` (maps each
file's ``package.ClassName`` → repo-relative path). Wildcard imports stay external.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import parallel_map, record_distinct
from ..treesitter import node_text, parse_source

_TYPE_DECLS = (
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
)

#: "com.acme.Foo" → repo-relative path, or ``None`` when >1 file declares the same FQCN
#: (ambiguous → honest-null). Built by :func:`build_fqcn_index`.
FqcnIndex = dict[str, str | None]


def _fqcn_index_one(args: tuple[str, str]) -> dict[str, str] | None:
    """Parse one Java file → ``{package.TypeName: rel}`` for its top-level types, reading the
    package **and** type names from the AST (reliable — no header-size limit, comment-safe).
    Pure, picklable worker for :func:`parallel_map`; ``None`` on read failure."""
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("java", source, 0).root_node
        package = ""
        for node in root.named_children:
            if node.type == "package_declaration":
                nm = next((c for c in node.named_children
                           if c.type in ("scoped_identifier", "identifier")), None)
                package = node_text(nm, source) if nm is not None else ""
                break
        frag: dict[str, str] = {}
        for node in root.named_children:
            if node.type in _TYPE_DECLS:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    name = node_text(nm, source)
                    frag[f"{package}.{name}" if package else name] = rel
        return frag
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="java",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files, jobs: int = 1) -> FqcnIndex:
    """Repo-level pre-pass: map each top-level type's fully-qualified name (package + type
    name, both from the AST) → repo path. A FQCN declared in >1 file collapses to ``None``
    (ambiguous → honest-null). Parses each file across ``jobs`` processes."""
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
    bindings: dict[str, str] = {}  # simple class/member name → in-repo file (calls[].path)

    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in node.children)
        is_wildcard = any(c.type == "asterisk" for c in node.children)
        scoped = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if scoped is None:
            continue
        fqcn = node_text(scoped, source)
        if is_wildcard:
            external.setdefault(fqcn + ".*", None)
            continue
        resolved = _resolve(fqcn, is_static, index)
        (internal if resolved else external).setdefault(resolved or fqcn, None)
        if resolved:  # `import a.b.Foo` → receiver "Foo"; `import static a.b.U.f` → "f"
            bindings[fqcn.rsplit(".", 1)[-1]] = resolved

    return list(internal), list(external), [], bindings  # Java has no explicit exports
