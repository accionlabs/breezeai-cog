"""Repo-wide PHP class-name index for certain import resolution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from tree_sitter import Node

from ...logging import get_logger
from ...utils import repo_relative
from ..additive import (
    collect_additive, collect_fact_sources, resolve_additive, split_fact_sources,
)
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
    #: Registered facts (``handler_sites``' ``php.method-lines``), read with ``index_fact``.
    facts: dict[str, Any] = field(default_factory=dict)

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


def namespace_of(root: Node, source: bytes) -> str:
    for node in root.named_children:
        if node.type == "namespace_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                return node_text(name, source).lstrip("\\")
    return ""


def _index_one(args: tuple[str, str, str]) -> tuple[dict[str, str], dict[str, Any]] | None:
    file_path, relative_path, repo_root = args
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
        return found, collect_additive("php", root, source, relative_path, Path(repo_root))
    except Exception as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_path, language="php",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PhpIndex:
    own, fact_files = split_fact_sources("php", list(files))
    collected: dict[str, list[Any]] = collect_fact_sources("php", fact_files, repo_root, jobs)
    args = [(str(path), repo_relative(path, repo_root), str(repo_root)) for path in own]
    fqcn: dict[str, str | None] = {}
    for fragment in parallel_map(args, _index_one, jobs):
        if fragment is None:
            continue
        names, facts = fragment
        for name, path in names.items():
            record_distinct(fqcn, name, path)
        for fact, value in facts.items():
            collected.setdefault(fact, []).append(value)
    folded_fqcn: dict[str, str | None] = {}
    namespace_members: dict[str, dict[str, str | None]] = {}
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
    index = PhpIndex(fqcn=fqcn, folded_fqcn=folded_fqcn, namespace_members=namespace_members)
    # Registered facts may read the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("php", collected, index))
