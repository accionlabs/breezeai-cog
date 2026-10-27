"""Repo-level pre-pass: lower-cased Apex type name → declaring file, plus registered facts.

Apex has no import statements — any class can call any other by name — so cross-file
call resolution needs this map. A name declared in more than one file maps to ``None``
(unresolvable), never to a guessed file. Facts registered for ``apex`` (``handler_sites``'
``apex.method-lines``) are collected in the same per-file pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ...logging import get_logger
from ...utils import repo_relative
from ..additive import collect_additive, resolve_additive
from ..index_common import parallel_map
from ..treesitter import node_text, parse_source
from .mappings import TYPE_DECLARATIONS


@dataclass(slots=True)
class ApexIndex:
    #: Lower-cased top-level type name → its file, ``None`` when declared in two files.
    classes: dict[str, str | None] = field(default_factory=dict)
    #: Registered facts (``apex.method-lines``), read with ``index_fact``.
    facts: dict[str, Any] = field(default_factory=dict)


_Fragment = tuple[dict[str, str], dict[str, Any]]


def _index_one(args: tuple[str, str, str]) -> _Fragment | None:
    """Type names and fact values of one file (picklable worker for ``parallel_map``)."""
    file_path, rel, repo_root = args
    try:
        source = Path(file_path).read_bytes()
        root = parse_source("apex", source).root_node
    except Exception as exc:
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_path, language="apex",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None
    names: dict[str, str] = {}
    for child in root.named_children:
        if child.type in TYPE_DECLARATIONS:
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                names[node_text(name_node, source).lower()] = rel
    return names, collect_additive("apex", root, source, rel, Path(repo_root))


def build_class_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ApexIndex:
    args = [(str(f), repo_relative(f, repo_root), str(repo_root)) for f in files]
    classes: dict[str, str | None] = {}
    collected: dict[str, list[Any]] = {}
    for fragment in parallel_map(args, _index_one, jobs):
        if fragment is None:
            continue
        names, facts = fragment
        for key, rel in names.items():
            classes[key] = rel if key not in classes or classes[key] == rel else None
        for fact, value in facts.items():
            collected.setdefault(fact, []).append(value)
    index = ApexIndex(classes=classes)
    return replace(index, facts=resolve_additive("apex", collected, index))
