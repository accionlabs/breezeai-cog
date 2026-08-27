"""Scala import extraction + FQCN resolution.

Scala imports resolve to in-repo files via a repo-level FQCN index built by ``build_index``
(maps each file's ``package.TypeName`` → repo-relative path). Wildcard imports stay external.
Selector groups (``{X, Y}``) and renames (``{X => Y}``) are supported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..constfold import Token
from ..index_common import parallel_map, record_distinct, seed_same_package
from ..treesitter import node_text, parse_source

_TYPE_DECLS = (
    "class_definition",
    "object_definition",
    "trait_definition",
    "enum_definition",
    "package_object",
)

#: "com.acme.Foo" → repo-relative path, or ``None`` when >1 file declares the same FQCN
#: (ambiguous → honest-null). Built by :func:`build_fqcn_index`.
FqcnIndex = dict[str, str | None]


@dataclass(frozen=True)
class ScalaIndex:
    """Repo-level pre-pass result: the FQCN → path map for import resolution."""

    fqcn: FqcnIndex = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)


def _package_of(root: Node, source: bytes) -> str:
    pkgs: list[str] = []
    for node in root.named_children:
        if node.type == "package_clause":
            nm = node.child_by_field_name("name")
            if nm is not None:
                txt = node_text(nm, source).strip()
                if txt:
                    pkgs.append(txt)
    return ".".join(pkgs)


def _collect_top_level_types(root: Node, source: bytes) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    package = _package_of(root, source)

    for child in root.named_children:
        if child.type in _TYPE_DECLS:
            nm = child.child_by_field_name("name")
            if nm is not None:
                results.append((package, node_text(nm, source)))
        elif child.type == "package_clause":
            sub_nm = child.child_by_field_name("name")
            sub_pkg = node_text(sub_nm, source).strip() if sub_nm is not None else ""
            for sub_child in child.named_children:
                if sub_child.type in _TYPE_DECLS:
                    nm = sub_child.child_by_field_name("name")
                    if nm is not None:
                        results.append((sub_pkg, node_text(nm, source)))

    return results


def _fqcn_index_one(args: tuple[str, str]) -> tuple[dict[str, str], dict[str, list[Token]]] | None:
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("scala", source, 0).root_node
        fqcn_frag: dict[str, str] = {}
        for package, name in _collect_top_level_types(root, source):
            fqcn_frag[f"{package}.{name}" if package else name] = rel
        return fqcn_frag, {}
    except Exception as exc:
        from ...logging import get_logger

        get_logger("breezeai_cog.index").warning(
            "index.file.skipped",
            path=file_s,
            language="scala",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ScalaIndex:
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    fqcn: FqcnIndex = {}
    for frag in parallel_map(args, _fqcn_index_one, jobs):
        if frag is None:
            continue
        fqcn_frag, _ = frag
        for name, rel in fqcn_frag.items():
            record_distinct(fqcn, name, rel)
    return ScalaIndex(fqcn=fqcn)


def _resolve(fqcn: str, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    return index.get(fqcn)


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: str | Path,
    index: FqcnIndex | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}

    for node in root.named_children:
        if node.type != "import_declaration":
            continue

        named = node.named_children
        if not named:
            continue

        last = named[-1]
        if last.type == "namespace_wildcard":
            prefix = ".".join(node_text(c, source) for c in named[:-1] if c.type == "identifier")
            external.setdefault(f"{prefix}.*" if prefix else "*", None)
        elif last.type == "namespace_selectors":
            prefix = ".".join(node_text(c, source) for c in named[:-1] if c.type == "identifier")
            for sel in last.named_children:
                if sel.type == "namespace_wildcard":
                    external.setdefault(f"{prefix}.*" if prefix else "*", None)
                elif sel.type == "identifier":
                    name = node_text(sel, source)
                    fqcn = f"{prefix}.{name}" if prefix else name
                    resolved = _resolve(fqcn, index)
                    (internal if resolved else external).setdefault(resolved or fqcn, None)
                    if resolved:
                        bindings[name] = resolved
                elif sel.type == "arrow_renamed_identifier":
                    ids = [c for c in sel.named_children if c.type == "identifier"]
                    if len(ids) >= 2:
                        orig_name = node_text(ids[0], source)
                        alias_name = node_text(ids[1], source)
                        fqcn = f"{prefix}.{orig_name}" if prefix else orig_name
                        resolved = _resolve(fqcn, index)
                        (internal if resolved else external).setdefault(resolved or fqcn, None)
                        if resolved:
                            bindings[alias_name] = resolved
                    elif ids:
                        name = node_text(ids[0], source)
                        fqcn = f"{prefix}.{name}" if prefix else name
                        resolved = _resolve(fqcn, index)
                        (internal if resolved else external).setdefault(resolved or fqcn, None)
                        if resolved:
                            bindings[name] = resolved
        else:
            ids = [c for c in named if c.type == "identifier"]
            if ids:
                fqcn = ".".join(node_text(c, source) for c in ids)
                resolved = _resolve(fqcn, index)
                (internal if resolved else external).setdefault(resolved or fqcn, None)
                if resolved:
                    bindings[node_text(ids[-1], source)] = resolved

    seed_same_package(bindings, _package_of(root, source), index)
    return list(internal), list(external), [], bindings
