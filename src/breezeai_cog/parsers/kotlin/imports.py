from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..constfold import Token
from ..index_common import parallel_map, record_distinct, seed_same_package
from ..treesitter import node_text, parse_source

_TYPE_DECLS = ("class_declaration", "object_declaration")
FqcnIndex = dict[str, str | None]


@dataclass(frozen=True)
class KotlinIndex:
    fqcn: FqcnIndex = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)


def _fqcn_index_one(args: tuple[str, str]) -> tuple[dict[str, str], dict[str, list[Token]]] | None:
    file_s, rel = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("kotlin", source, 0).root_node
        package = ""
        for node in root.named_children:
            if node.type == "package_header":
                pkg = next((c for c in node.named_children if c.type == "identifier"), None)
                if pkg is not None:
                    package = node_text(pkg, source)
                break
        fqcn_frag: dict[str, str] = {}
        for node in root.named_children:
            if node.type in _TYPE_DECLS:
                nm = next((c for c in node.named_children if c.type == "type_identifier"), None)
                if nm is not None:
                    name = node_text(nm, source)
                    fqcn_frag[f"{package}.{name}" if package else name] = rel
        return fqcn_frag, {}
    except Exception:
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> KotlinIndex:
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    fqcn: FqcnIndex = {}
    for frag in parallel_map(args, _fqcn_index_one, jobs):
        if frag is None:
            continue
        fqcn_frag, _ = frag
        for name, rel in fqcn_frag.items():
            record_distinct(fqcn, name, rel)
    return KotlinIndex(fqcn=fqcn)


def _package_of(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "package_header":
            pkg = next((c for c in node.named_children if c.type == "identifier"), None)
            if pkg is not None:
                return node_text(pkg, source)
    return ""


def _resolve(fqcn: str, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    return index.get(fqcn)


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path, index: FqcnIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}

    for node in root.named_children:
        if node.type != "import_list":
            continue
        for imp in node.named_children:
            if imp.type != "import_header":
                continue
            identifier = None
            for child in imp.children:
                if child.type == "identifier":
                    identifier = child
                    break
            if identifier is None:
                continue
            fqcn = node_text(identifier, source)
            resolved = _resolve(fqcn, index)
            if resolved:
                internal.setdefault(resolved, None)
                bindings[fqcn.rsplit(".", 1)[-1]] = resolved
            else:
                external.setdefault(fqcn, None)

    seed_same_package(bindings, _package_of(root, source), index)
    return list(internal), list(external), [], bindings
