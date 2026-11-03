"""CParser — C files, read by the extraction C and C++ share (``parsers/cfamily_common``).

C is parsed with the tree-sitter **C** grammar: C++ reads valid C differently — ``int new;`` or
``f(int class)`` is an error to it, and an old-style (K&R) definition is not a function. Every
C construct the shared extraction reads has the same node type in both grammars, and the
C++-only ones (classes, namespaces, templates) simply never occur.

What a C file yields: ``#include`` → imports; ``struct`` / ``union`` / ``enum`` (also through
``typedef``) → a ``Class``; a function definition → a ``Function``; macros, prototypes,
``typedef`` aliases and file-scope variables → statements.

C and C++ both register ``.h``; the ownership step decides each header's language from the
files that include it. C reads the C++ index after its own, for the C++ functions defined
inside ``extern "C"`` — the only C++ functions a C file can call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ..additive import run_additive
from ..base import Ownership, ParseContext
from ..cfamily_common.imports import include_targets
from ..cfamily_common.index import CFamilyIndex
from ..cfamily_common.parser import CFamilyParser
from ...schemas import FileRecord
from .index import build_c_index


class CParser(CFamilyParser):
    name = "c"
    grammar = "c"
    extensions = (".c", ".h")
    related_languages = ("cpp",)
    ownership = Ownership(references=include_targets)

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
    ) -> CFamilyIndex:
        return build_c_index(Path(repo_root), files, jobs, parse_timeout_micros)

    def callable_in(self, related: CFamilyIndex) -> dict[str, str | None]:
        """C can call only the C++ functions with C linkage."""
        return related.c_linkage

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        run_additive("c", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
