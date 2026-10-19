"""Repo-wide PHP class-name index for certain import resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...logging import get_logger
from ...utils import repo_relative
from ..index_common import parallel_map, record_distinct
from ..treesitter import node_text, parse_source

_TYPE_DECLARATIONS = {
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
}


@dataclass(frozen=True, slots=True)
class PhpIndex:
    fqcn: dict[str, str | None] = field(default_factory=dict)


def _namespace(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "namespace_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                return node_text(name, source).lstrip("\\")
    return ""


def _index_one(args: tuple[str, str]) -> dict[str, str] | None:
    file_path, relative_path = args
    try:
        source = Path(file_path).read_bytes()
    except OSError as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_path, language="php",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None
    try:
        root = parse_source("php", source, 0).root_node
        namespace = _namespace(root, source)
        found: dict[str, str] = {}

        def visit(node: Node) -> None:
            if node.type in _TYPE_DECLARATIONS:
                name = node.child_by_field_name("name")
                if name is not None:
                    simple = node_text(name, source)
                    found[f"{namespace}\\{simple}" if namespace else simple] = relative_path
                    return
            for child in node.named_children:
                visit(child)

        visit(root)
        return found
    except Exception as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_path, language="php",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PhpIndex:
    args = [(str(path), repo_relative(path, repo_root)) for path in files]
    fqcn: dict[str, str | None] = {}
    for fragment in parallel_map(args, _index_one, jobs):
        if fragment is not None:
            for name, path in fragment.items():
                record_distinct(fqcn, name, path)
    return PhpIndex(fqcn=fqcn)
