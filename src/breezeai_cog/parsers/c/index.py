"""C's repo-wide index: the C++ index (``parsers/cpp/index.py``) built over C files with the C
grammar. Its registered facts are C's own: a detector registered for ``c`` collects here."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from tree_sitter import Node

from ..additive import collect_additive, resolve_additive
from ..cpp.index import CppIndex, build_cpp_index


def collect_c_facts(root: Node, source: bytes, rel: str, repo_root: Path) -> dict[str, Any]:
    """What C's registered facts collect from one C file."""
    return collect_additive("c", root, source, rel, repo_root)


def resolve_c_facts(collected: dict[str, list[Any]], index: CppIndex) -> dict[str, Any]:
    """C's registered facts, resolved once over every file's collection."""
    return resolve_additive("c", collected, index)


def build_c_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
) -> CppIndex:
    return build_cpp_index(
        repo_root, files, jobs, "c", language="c", collect=collect_c_facts,
        resolve=resolve_c_facts, parse_timeout_micros=parse_timeout_micros,
    )
