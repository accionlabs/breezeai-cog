"""C++ ``#include`` extraction + in-repo header resolution.

A ``preproc_include``'s ``path`` field is either a ``system_lib_string`` (``<string>`` —
always external) or a ``string_literal`` (``"apply.h"`` — a local include we try to
resolve to a repo file). Resolution uses a repo-wide **header index** (built once by
``build_index``): a header basename → its repo-relative path, collapsed to ``None`` when
more than one file shares that basename (ambiguous → honest-null, left external rather
than joined to a guessed file). An unresolved local include is emitted as an external
import named by its include path — never as a fabricated in-repo edge."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import record_distinct
from ..treesitter import node_text

#: header basename (``apply.h``) → repo-relative path, or ``None`` when >1 file declares
#: that basename (ambiguous → honest-null). Built by :func:`build_header_index`.
HeaderIndex = dict[str, str | None]


def build_header_index(repo_root: Path, files: Sequence[Path]) -> HeaderIndex:
    """basename → repo-relative path over all C++ files, honest-null on a basename
    shared by >1 file. No parse needed — this is a filename map."""
    index: HeaderIndex = {}
    for f in files:
        rel = repo_relative(f, repo_root)
        record_distinct(index, Path(rel).name, rel)
    return index


def _include_string(path_node: Node, source: bytes) -> str:
    """The include target text from a ``string_literal`` path (``"a/b.h"`` → ``a/b.h``)."""
    frag = next((c for c in path_node.named_children if c.type == "string_content"), None)
    return node_text(frag, source) if frag is not None else node_text(path_node, source).strip('"')


def _walk_includes(node: Node) -> Iterator[Node]:
    """Yield every ``preproc_include`` in the tree (they may sit inside ``#ifdef`` blocks)."""
    for child in node.named_children:
        if child.type == "preproc_include":
            yield child
        else:
            yield from _walk_includes(child)


def extract_imports(
    root: Node, source: bytes, index: HeaderIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}

    for inc in _walk_includes(root):
        path_node = inc.child_by_field_name("path")
        if path_node is None:
            continue
        if path_node.type == "system_lib_string":  # <vector> — always external
            external.setdefault(node_text(path_node, source), None)
            continue
        if path_node.type != "string_literal":
            continue
        target = _include_string(path_node, source)
        resolved = index.get(Path(target).name) if index else None
        if resolved:
            internal.setdefault(resolved, None)
        else:  # unresolved / ambiguous local include → external, named by its include path
            external.setdefault(target, None)

    # C++ has no explicit exports, and headers don't bind a call symbol → file, so call
    # resolution stays same-file only (bindings empty → honest-null cross-file).
    return list(internal), list(external), [], {}
