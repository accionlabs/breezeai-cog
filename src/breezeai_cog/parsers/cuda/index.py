"""CUDA's repo-wide index: the index C and C++ share (``parsers/cfamily_common/index.py``),
built over CUDA files with the CUDA grammar. Its registered facts are CUDA's own: a detector
registered for ``cuda`` collects here."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from tree_sitter import Node

from ..additive import collect_additive, resolve_additive
from ..cfamily_common.index import CFamilyIndex, build_cfamily_index


def collect_cuda_facts(root: Node, source: bytes, rel: str, repo_root: Path) -> dict[str, Any]:
    """What CUDA's registered facts collect from one CUDA file."""
    return collect_additive("cuda", root, source, rel, repo_root)


def resolve_cuda_facts(collected: dict[str, list[Any]], index: CFamilyIndex) -> dict[str, Any]:
    """CUDA's registered facts, resolved once over every file's collection."""
    return resolve_additive("cuda", collected, index)


def build_cuda_index(
    repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
) -> CFamilyIndex:
    return build_cfamily_index(
        repo_root, files, jobs, grammar="cuda", language="cuda", collect=collect_cuda_facts,
        resolve=resolve_cuda_facts, parse_timeout_micros=parse_timeout_micros,
    )
