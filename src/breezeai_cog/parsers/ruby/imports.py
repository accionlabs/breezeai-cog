"""Ruby import extraction + repo-local resolution."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text


def _iter(node: Node):
    yield node
    for child in node.named_children:
        yield from _iter(child)


def _resolve_require(req: str, file_path: str, repo_root: Path) -> str | None:
    if not req:
        return None
    abs_file = repo_root / file_path
    if req.startswith("./") or req.startswith("../"):
        target = (abs_file.parent / req).resolve()
    else:
        target = repo_root / req
    candidates = [target]
    if not target.suffix:
        candidates.extend([target.with_suffix(".rb"), target / "index.rb"])
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix == ".rb":
            return repo_relative(candidate, repo_root)
    return None


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: str | Path,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    repo_root = Path(repo_root)
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    exports: list[str] = []
    bindings: dict[str, str] = {}

    for node in _iter(root):
        if node.type != "call":
            continue
        name = None
        args: list[str] = []
        for child in node.named_children:
            if child.type in {"identifier", "constant"}:
                name = node_text(child, source)
            elif child.type == "argument_list":
                for arg in child.named_children:
                    if arg.type in {"string", "string_content"}:
                        text = node_text(arg, source).strip('"\'')
                        args.append(text)
        if name not in {"require", "require_relative"}:
            continue
        for arg in args:
            resolved = _resolve_require(arg, file_path, repo_root) if name == "require_relative" else None
            if resolved is not None:
                internal.setdefault(resolved, None)
                bindings[arg.rsplit("/", 1)[-1].rsplit(".", 1)[0]] = resolved
            else:
                # bare require strings are treated as external unless they resolve to repo files.
                external.setdefault(arg, None)
    return list(internal), list(external), exports, bindings
