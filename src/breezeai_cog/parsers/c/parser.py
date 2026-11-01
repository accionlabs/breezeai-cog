"""CParser — extracts one C translation unit into a FileRecord.

C is parsed with the tree-sitter **C** grammar and the C++ parser's extraction
(``parsers/cpp``): every C construct the extraction reads has the same node type in both
grammars, and the C++-only ones (classes, namespaces, templates) simply never occur. The C
grammar matters because C++ reads valid C differently — ``int new;`` or ``f(int class)`` is an
error to it, and an old-style (K&R) definition is not a function.

What a C file yields: ``#include`` → imports; ``struct`` / ``union`` / ``enum`` → a ``Class``;
a function definition → a ``Function``; ``typedef`` and file-scope variables → statements.

C and C++ both register ``.h``; the ownership step decides each header's language
(``parsers/cpp/ownership.py``). C reads the C++ index after its own, for the C++ functions
defined inside ``extern "C"`` — the only C++ functions a C file can call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ..additive import run_additive
from ..base import Ownership, ParseContext
from ..cpp.index import CppIndex
from .index import build_c_index
from ..cpp.ownership import include_targets
from ..cpp.parser import CppParser
from ...schemas import FileRecord


class CParser(CppParser):
    name = "c"
    grammar = "c"
    extensions = (".c", ".h")
    related_languages = ("cpp",)
    ownership = Ownership(references=include_targets)

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
    ) -> CppIndex:
        return build_c_index(Path(repo_root), files, jobs, parse_timeout_micros)

    def callable_in(self, related: CppIndex) -> dict[str, str | None]:
        """C can call only the C++ functions with C linkage."""
        return related.c_linkage

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        run_additive("c", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
