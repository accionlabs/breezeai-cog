"""Go import extraction + repo-level resolver index."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ..treesitter import node_text


@dataclass(frozen=True)
class GoIndex:
    modules: dict[str, str | None] = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)


def _module_path(gomod: Path) -> str | None:
    try:
        text = gomod.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.strip().startswith("module "):
            return line.split(None, 1)[1].strip().strip('"')
    return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> GoIndex:
    """Map each package import path to a file of that package. A file belongs to the module
    of the nearest ``go.mod`` above it, so a repo with several modules (``services/api/go.mod``,
    ``tools/go.mod``) resolves imports within each of them. Reads the ``go.mod`` files only —
    no ``.go`` file is opened."""
    repo_root = Path(repo_root)
    module_dirs: dict[str, str] = {}  # repo-relative dir ("" = root) -> module path
    gomods = [Path(f) for f in files if Path(f).name == "go.mod"]
    if (repo_root / "go.mod").is_file():
        gomods.append(repo_root / "go.mod")
    for gomod in gomods:
        try:
            rel_dir = gomod.parent.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        module = _module_path(gomod)
        if module:
            module_dirs["" if rel_dir == "." else rel_dir] = module

    modules: dict[str, str | None] = {}
    if not module_dirs:
        return GoIndex(modules=modules)
    for file_path in sorted(files, key=lambda item: Path(item).as_posix()):
        path = Path(file_path)
        if path.suffix != ".go":
            continue
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        package_dir = relative.rpartition("/")[0]
        root_dir = package_dir
        while root_dir not in module_dirs and root_dir:
            root_dir = root_dir.rpartition("/")[0]
        module = module_dirs.get(root_dir)
        if module is None:
            continue
        suffix = package_dir[len(root_dir):].lstrip("/")
        modules.setdefault(module + ("/" + suffix if suffix else ""), relative)
    return GoIndex(modules=modules)


def extract_imports(
    root: Node,
    source: bytes,
    index: GoIndex | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: set[str] = set()
    external: set[str] = set()
    bindings: dict[str, str] = {}

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
                target = index.modules[module]
                if target is not None:
                    internal.add(target)
            elif module.startswith(".") or module.startswith("/"):
                internal.add(module)
            else:
                external.add(module)

    return sorted(internal), sorted(external), [], bindings
