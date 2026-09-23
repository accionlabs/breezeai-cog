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
            current = modules.get(import_path)
            modules[import_path] = relative if current is None and import_path not in modules else None
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
