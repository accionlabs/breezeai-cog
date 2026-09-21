"""Go import extraction + repo-level resolver index."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ..treesitter import node_text, parse_source


@dataclass(frozen=True)
class GoIndex:
    modules: dict[str, str | None] = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> GoIndex:
    """Go packages do not use the Java/C# FQCN model, but keeping a minimal index
    preserves the repository's expected contract for import resolution and same-package
    bindings without adding new dependencies or runtime complexity."""
    module = None
    mod_path = repo_root / "go.mod"
    if mod_path.is_file():
        for line in mod_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("module "):
                module = line.split(None, 1)[1].strip()
                break
    modules: dict[str, str | None] = {}
    if module:
        for file_path in files:
            path = Path(file_path)
            if path.suffix != ".go":
                continue
            try:
                relative = path.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            import_path = module + ("/" + str(Path(relative).parent).replace("\\", "/") if str(Path(relative).parent) != "." else "")
            modules[import_path] = relative
    return GoIndex(modules=modules)


def _package_name(root: Node, source: bytes) -> str:
    for child in root.named_children:
        if child.type == "package_clause":
            name = child.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
    return ""


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: str | Path,
    index: GoIndex | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: set[str] = set()
    external: set[str] = set()
    bindings: dict[str, str] = {}
    package_name = _package_name(root, source)

    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        for spec in child.named_children:
            if spec.type != "import_spec":
                continue
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            module = node_text(path_node, source).strip('"')
            alias_node = spec.child_by_field_name("name")
            alias = node_text(alias_node, source) if alias_node is not None else None
            if alias:
                bindings[alias] = module
            if index is not None and module in index.modules:
                internal.add(index.modules[module] or module)
            elif module.startswith(".") or module.startswith("/"):
                internal.add(module)
            else:
                external.add(module)

    if package_name:
        # same-package aliasing should be possible without import edges, but a concrete
        # repo-local path is not inferable from the Go grammar alone. Keep the binding map
        # honest: a same-package import is not an in-repo path, only an external module.
        pass

    return sorted(internal), sorted(external), [], bindings
