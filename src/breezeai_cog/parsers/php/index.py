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
    folded_fqcn: dict[str, str | None] = field(default_factory=dict)
    namespace_members: dict[str, dict[str, str | None]] = field(default_factory=dict)
    simple_names: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep hand-built indexes used by parser callers/tests equivalent to the
        # repository index produced below.
        if not self.folded_fqcn:
            folded: dict[str, str | None] = {}
            for name, path in self.fqcn.items():
                if name.casefold() in folded and folded[name.casefold()] != path:
                    folded[name.casefold()] = None
                else:
                    folded[name.casefold()] = path
            object.__setattr__(self, "folded_fqcn", folded)
        if not self.namespace_members:
            members: dict[str, dict[str, str | None]] = {}
            for name, path in self.fqcn.items():
                namespace, _, simple = name.rpartition("\\")
                target = members.setdefault(namespace, {})
                if simple in target and target[simple] != path:
                    target[simple] = None
                else:
                    target[simple] = path
            object.__setattr__(self, "namespace_members", members)
        if not self.simple_names:
            simple_names: dict[str, str | None] = {}
            for name, path in self.fqcn.items():
                simple = name.rsplit("\\", 1)[-1].casefold()
                if simple in simple_names and simple_names[simple] != path:
                    simple_names[simple] = None
                else:
                    simple_names[simple] = path
            object.__setattr__(self, "simple_names", simple_names)


def namespace_of(root: Node, source: bytes) -> str:
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
        namespace = namespace_of(root, source)
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
    folded_fqcn: dict[str, str | None] = {}
    namespace_members: dict[str, dict[str, str | None]] = {}
    simple_names: dict[str, str | None] = {}
    for name, indexed_path in fqcn.items():
        folded = name.casefold()
        existing = folded_fqcn.get(folded)
        if folded in folded_fqcn and existing != indexed_path:
            folded_fqcn[folded] = None
        else:
            folded_fqcn[folded] = indexed_path
        namespace, _, simple = name.rpartition("\\")
        members = namespace_members.setdefault(namespace, {})
        if simple in members and members[simple] != indexed_path:
            members[simple] = None
        else:
            members[simple] = indexed_path
        simple_key = simple.casefold()
        if simple_key in simple_names and simple_names[simple_key] != indexed_path:
            simple_names[simple_key] = None
        else:
            simple_names[simple_key] = indexed_path
    return PhpIndex(
        fqcn=fqcn,
        folded_fqcn=folded_fqcn,
        namespace_members=namespace_members,
        simple_names=simple_names,
    )
