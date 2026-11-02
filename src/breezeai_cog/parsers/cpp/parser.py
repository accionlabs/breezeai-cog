"""CppParser — C++ files, read by the extraction C and C++ share (``parsers/cfamily_common``).

What C++ adds to it: the C++ grammar, its extensions, its own index and registered facts
(``index.py``), and its ownership proof for ``.h`` (``ownership.py``) — syntax C does not have.
``.h`` is C++'s by default when nothing else decides it. C++ reads the C index after its own, so
a call or ``#include`` from C++ resolves into C files: C++ can call every C function.
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
from .index import build_cpp_index
from .ownership import cpp_only_syntax


class CppParser(CFamilyParser):
    name = "cpp"
    grammar = "cpp"
    extensions: tuple[str, ...] = (
        ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h", ".hh", ".hxx", ".ipp", ".inl",
    )
    related_languages = ("c",)
    ownership = Ownership(
        proves=cpp_only_syntax, references=include_targets, includes=("c",), default_for=(".h",),
    )

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
    ) -> CFamilyIndex:
        """Repo-wide index: the ``#include`` header map plus a parse pre-pass mapping
        free-function / ``Scope::method`` **definitions** → path (for cross-file call
        resolution). Honest-null on any name shared by >1 file."""
        return build_cpp_index(Path(repo_root), files, jobs, parse_timeout_micros)

    def callable_in(self, related: CFamilyIndex) -> dict[str, str | None]:
        """C++ can call every C function."""
        return related.funcs

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        run_additive("cpp", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
