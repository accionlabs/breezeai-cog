"""VB.NET import extraction.

Like C#, VB ``Imports`` names a **namespace** (or an alias), not a file — so every
import is recorded external and calls resolve same-file only (precision-first).

The one cross-file fact we *do* index is class heritage (:func:`build_vb_index`): a
controller can inherit its ``<Route>``/``<Authorize>`` from a base declared in another
file, and the shared :func:`~breezeai_cog.parsers.csharp_aspnet.routes.detect_controller_routes`
walks that chain via ``index.class_heritage``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ..index_common import ClassHeritage, parallel_map, record_heritage
from ..treesitter import node_text, parse_source


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


@dataclass
class VbIndex:
    """Repo-wide VB heritage index — simple class name → its base + attributes, for
    cross-file base-controller resolution (ASP.NET route/auth inheritance). A value of
    ``None`` marks an ambiguous name (declared by >1 class with differing bases): callers
    must not resolve through it (honest-null). Exposes ``class_heritage`` — the same slice
    of the C# index that ``detect_controller_routes`` reads via ``getattr(index, …)``."""

    class_heritage: dict[str, ClassHeritage | None] = field(default_factory=dict)


def _vb_index_one(file_s: str) -> dict[str, ClassHeritage | None] | None:
    """Parse one VB file into its partial ``class_heritage`` map — pure, picklable worker
    for :func:`parallel_map`. Returns ``None`` on read failure."""
    # Lazy imports: parser.py imports this module, so a top-level import would cycle.
    from .classes import _heritage
    from .functions import attributes_from_blocks
    from .parser import iter_type_declarations

    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    root = parse_source("vb", source, 0).root_node
    heritage: dict[str, ClassHeritage | None] = {}
    for block, attrs in iter_type_declarations(root):
        name_node = block.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else None
        if not name:
            continue
        record_heritage(heritage, name, _heritage(block, source)[0], attributes_from_blocks(attrs, source))
    return heritage


def build_vb_index(repo_root: Path, files, jobs: int = 1) -> VbIndex:
    """Repo-level pre-pass: map each declared VB type's simple name → its heritage (base
    class + attributes), parsing each file across ``jobs`` processes. VB ``Imports`` name
    namespaces (no namespace→file map), so only heritage is indexed — enough for ASP.NET
    cross-file base-controller resolution."""
    index = VbIndex()
    for frag in parallel_map([str(f) for f in files], _vb_index_one, jobs):
        if not frag:
            continue
        for name, ch in frag.items():
            if ch is None:
                index.class_heritage[name] = None
            else:
                record_heritage(index.class_heritage, name, ch.extends, ch.decorators)
    return index
