"""VB.NET import extraction.

Like C#, VB ``Imports`` names a **namespace** (or an alias), not a file — so every
import is recorded external and calls resolve same-file only (precision-first).

The index (:func:`build_vb_index`) holds class heritage — a controller can inherit its
``<Route>``/``<Authorize>`` from a base declared in another file, and the shared
:func:`~breezeai_cog.parsers.aspnet_common.detect_controller_routes` walks that chain via
``index.class_heritage`` — and the facts registered for ``vb`` (``register_fact``), collected
in the same parse."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...utils import repo_relative
from ..additive import collect_additive, resolve_additive
from ..index_common import ClassHeritage, parallel_map, record_heritage
from ..treesitter import node_text
from .source import parse_nested, parse_vb


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
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


#: One file's share of the index: its heritage, and each fact's values (one per parse level).
_IndexFrag = tuple[dict[str, ClassHeritage | None], dict[str, list[Any]]]


def _vb_index_one(args: tuple[str, str, str]) -> _IndexFrag | None:
    """Parse one VB file into its partial ``class_heritage`` map and its registered facts —
    pure, picklable worker for :func:`parallel_map`. Returns ``None`` on read failure."""
    # Lazy imports: parser.py imports this module, so a top-level import would cycle.
    from .classes import _heritage
    from .functions import attributes_from_blocks
    from .parser import iter_type_declarations

    file_s, rel, root_s = args
    repo_root = Path(root_s)
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        roots = [parse_vb(source, 0).root_node, *(lv.root for lv in parse_nested(source, 0))]
        heritage: dict[str, ClassHeritage | None] = {}
        for block, attrs in (t for root in roots for t in iter_type_declarations(root)):
            name_node = block.child_by_field_name("name")
            name = node_text(name_node, source) if name_node is not None else None
            if not name:
                continue
            record_heritage(heritage, name, _heritage(block, source)[0], attributes_from_blocks(attrs, source))
        # A nested type's body is blank in the outer tree: each level is collected on its own.
        facts: dict[str, list[Any]] = {}
        for root in roots:
            for name, value in collect_additive("vb", root, source, rel, repo_root).items():
                facts.setdefault(name, []).append(value)
        return heritage, facts
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="vb",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_vb_index(repo_root: Path, files, jobs: int = 1) -> VbIndex:
    """Repo-level pre-pass: map each declared VB type's simple name → its heritage (base
    class + attributes), parsing each file across ``jobs`` processes. VB ``Imports`` name
    namespaces (no namespace→file map), so heritage is indexed — enough for ASP.NET
    cross-file base-controller resolution — plus the facts registered for ``vb``."""
    index = VbIndex()
    collected: dict[str, list[Any]] = {}
    args = [(str(f), repo_relative(f, repo_root), str(repo_root)) for f in files]
    for frag in parallel_map(args, _vb_index_one, jobs):
        if not frag:
            continue
        heritage, facts = frag
        for name, values in facts.items():
            collected.setdefault(name, []).extend(values)
        for name, ch in heritage.items():
            if ch is None:
                index.class_heritage[name] = None
            else:
                record_heritage(index.class_heritage, name, ch.extends, ch.decorators)
    # Registered facts may need the whole index, so they resolve after it is built.
    index.facts = resolve_additive("vb", collected, index)
    return index
