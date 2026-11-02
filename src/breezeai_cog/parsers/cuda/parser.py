"""CudaParser — CUDA files (``.cu``, ``.cuh``), read by the extraction C and C++ share
(``parsers/cfamily_common``).

CUDA is C++ with GPU extensions, parsed with the tree-sitter **CUDA** grammar, which is built on
the C++ one: the constructs the shared extraction reads keep their C++ node types, and the
extensions are additions. ``__global__`` / ``__device__`` / ``__host__`` are qualifiers, so a
kernel is a ``Function`` like any other, and a launch ``scale<<<grid, block>>>(x)`` is a call to
``scale``.

Only CUDA registers ``.cu`` and ``.cuh``, so it takes no part in deciding who owns ``.h``. It
reads the C++ and C indexes after its own: a ``.cu`` file includes C and C++ headers and calls
their functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ..additive import run_additive
from ..base import ParseContext
from ..cfamily_common.index import CFamilyIndex
from ..cfamily_common.parser import CFamilyParser
from ...schemas import FileRecord
from .index import build_cuda_index


class CudaParser(CFamilyParser):
    name = "cuda"
    grammar = "cuda"
    extensions = (".cu", ".cuh")
    related_languages = ("cpp", "c")

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1, parse_timeout_micros: int = 0
    ) -> CFamilyIndex:
        return build_cuda_index(Path(repo_root), files, jobs, parse_timeout_micros)

    def callable_in(self, related: CFamilyIndex) -> dict[str, str | None]:
        """CUDA can call every C++ and C function."""
        return related.funcs

    def run_detectors(self, root: Node, ctx: ParseContext, record: FileRecord) -> None:
        run_additive("cuda", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
