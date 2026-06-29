"""Java import extraction + FQCN resolution.

Java imports are fully-qualified class names (``com.acme.OrderRepo``). They resolve
to in-repo files via a repo-level **FQCN index** built by ``build_index`` (maps each
file's ``package.ClassName`` → repo-relative path). Wildcard imports stay external.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text

_PACKAGE_RE = re.compile(rb"^\s*package\s+([\w.]+)\s*;", re.M)

FqcnIndex = dict[str, str]  # "com.acme.Foo" -> "src/main/java/com/acme/Foo.java"


def build_fqcn_index(repo_root: Path, files) -> FqcnIndex:
    """Light scan: package (regex) + filename stem → FQCN → repo-relative path."""
    index: FqcnIndex = {}
    for file in files:
        try:
            head = Path(file).read_bytes()[:4096]
        except OSError:
            continue
        match = _PACKAGE_RE.search(head)
        package = match.group(1).decode("ascii", "ignore") if match else ""
        stem = Path(file).stem
        fqcn = f"{package}.{stem}" if package else stem
        index[fqcn] = repo_relative(file, repo_root)
    return index


def _resolve(fqcn: str, is_static: bool, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    if fqcn in index:
        return index[fqcn]
    if is_static and "." in fqcn:  # static import: strip the member to get the class
        cls = fqcn.rsplit(".", 1)[0]
        if cls in index:
            return index[cls]
    return None


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path, index: FqcnIndex | None = None
) -> tuple[list[str], list[str], list[str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}

    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in node.children)
        is_wildcard = any(c.type == "asterisk" for c in node.children)
        scoped = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if scoped is None:
            continue
        fqcn = node_text(scoped, source)
        if is_wildcard:
            external.setdefault(fqcn + ".*", None)
            continue
        resolved = _resolve(fqcn, is_static, index)
        (internal if resolved else external).setdefault(resolved or fqcn, None)

    return list(internal), list(external), []  # Java has no explicit exports
