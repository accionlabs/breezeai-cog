"""Import extraction + in-repo resolution.

Internal imports (resolved to repo-relative ``.py`` / package ``__init__.py`` paths)
drive the IMPORTS edge; everything else is recorded as an external/library import.
``__all__`` (if present) becomes ``exports``.

**Import roots.** An absolute import resolves from the repository root first. A repository often
holds several apps, each run from its own folder (that folder is on ``sys.path``)::

    etl/hubexo-etl/dags/eu/planning_dag.py:   from src.constants import REGION
    etl/hubexo-etl/src/constants.py           ← what it means (the app runs from hubexo-etl/)
    etl/harvestor-v2/src/constants.py         ← another app's: never picked

So when the root does not resolve it, the import is looked up where Python could find it from
this file (:class:`_Roots`), and linked only when exactly one place has it:

* a **dotted** import (``src.constants``): every folder above the file that is not itself a
  package (no ``__init__.py`` — what a ``sys.path`` entry looks like), and the ``src/`` of a
  declared project above it (``pyproject.toml`` / ``setup.py`` / ``setup.cfg``);
* a **single-part** import (``worker``): only the file's own folder when it is a script (not
  in a package — Python puts it first on ``sys.path``), or a declared project above it. Single
  names (``utils``, ``config``, ``transformers``) are the ones an installed package most often
  shares with a local file, so the folders above are not searched for them.

Only ancestors are searched, so a sibling app is never linked; a module found at two places is
ambiguous and stays external. Checked on two multi-app repositories: 2,675 links, every imported
name present in the linked file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text


def _dotted(node: Node, source: bytes) -> str:
    return node_text(node, source)


_PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def _module_file(base: Path, parts: list[str]) -> Path | None:
    target = base.joinpath(*parts)
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


#: Whether a name check must confirm the exact case: macOS and Windows file systems match
#: ``Utils.py`` for ``utils.py``; Python's import does not.
_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


class _Roots:
    """The folders an absolute import of one file may resolve under, beyond the repository root
    (see the module docstring). One per file. Each folder is asked about the few names that
    decide (the module, ``__init__.py``, a project file), never listed whole: the cost of an
    import is the number of folders above the file, not their size. Answers are memoised for
    this file's imports."""

    def __init__(self, file_path: str, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.folder = (repo_root / file_path).parent
        self._seen: dict[tuple[Path, str], bool] = {}
        self._listing: dict[Path, frozenset[str]] = {}
        self._above: list[Path] | None = None

    def _has(self, folder: Path, name: str) -> bool:
        """Whether ``folder`` holds an entry named exactly ``name``."""
        key = (folder, name)
        found = self._seen.get(key)
        if found is None:
            found = os.path.exists(os.path.join(folder, name))
            if found and _CASE_INSENSITIVE_FS:
                if folder not in self._listing:
                    try:
                        self._listing[folder] = frozenset(os.listdir(folder))
                    except OSError:
                        self._listing[folder] = frozenset()
                found = name in self._listing[folder]
            self._seen[key] = found
        return found

    def _ancestors(self) -> list[Path]:
        """The file's folder up to (not including) the repository root, which is tried first."""
        if self._above is None:
            self._above = []
            folder = self.folder
            while folder != self.repo_root and self.repo_root in folder.parents:
                self._above.append(folder)
                folder = folder.parent
        return self._above

    def _is_package(self, folder: Path) -> bool:
        return self._has(folder, "__init__.py")

    def _declares_project(self, folder: Path) -> bool:
        return any(self._has(folder, name) for name in _PROJECT_FILES)

    def _holds(self, folder: Path, first: str) -> bool:
        return self._has(folder, first) or self._has(folder, f"{first}.py")

    def candidates(self, parts: list[str]) -> list[Path]:
        # The module's first name is asked first: most imports that reach here are the standard
        # library's or an installed package's, found in no folder above the file.
        first = parts[0]
        folders: list[Path] = []
        for folder in self._ancestors():
            here = self._holds(folder, first)
            src = self._has(folder, "src") and self._holds(folder / "src", first)
            if not (here or src):
                continue
            if self._declares_project(folder):
                if here:
                    folders.append(folder)
                if src and not self._is_package(folder / "src"):
                    folders.append(folder / "src")
            elif here and len(parts) > 1 and not self._is_package(folder):
                folders.append(folder)
        if (len(parts) == 1 and self.folder not in folders and self._holds(self.folder, first)
                and not self._is_package(self.folder)):
            folders.insert(0, self.folder)  # a script: its own folder is on sys.path
        return folders

    def resolve(self, parts: list[str]) -> Path | None:
        found = {f for folder in self.candidates(parts) if (f := _module_file(folder, parts))}
        return found.pop() if len(found) == 1 else None


def _resolve(module: str, level: int, file_path: str, repo_root: Path,
             roots: _Roots | None = None) -> str | None:
    """Resolve a (possibly relative) module to an in-repo file path, or None."""
    abs_file = repo_root / file_path
    parts = [p for p in module.split(".") if p]
    if level > 0:
        base = abs_file.parent
        for _ in range(level - 1):
            base = base.parent
        target = base.joinpath(*parts) if parts else base
        found = next((c for c in (target.with_suffix(".py"), target / "__init__.py")
                      if c.is_file()), None)
    else:
        found = _module_file(repo_root, parts) if parts else None
        if found is None and parts:
            found = (roots or _Roots(file_path, repo_root)).resolve(parts)
    return repo_relative(found, repo_root) if found is not None else None


def _iter(node: Node):
    yield node
    for child in node.named_children:
        yield from _iter(child)


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    repo_root = Path(repo_root)
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    exports: list[str] = []
    bindings: dict[str, str] = {}  # local symbol/module name → in-repo file (calls[].path)
    roots = _Roots(file_path, repo_root)  # where an absolute import may resolve beyond the root

    for node in _iter(root):
        if node.type == "import_statement":
            for child in node.named_children:
                aliased = child.type == "aliased_import"
                target = child.named_children[0] if aliased else child
                if target.type != "dotted_name":
                    continue
                module = _dotted(target, source)
                resolved = _resolve(module, 0, file_path, repo_root, roots)
                (internal if resolved else external).setdefault(resolved or module, None)
                if resolved:  # `import m` → receiver "m"; `import m as mm` → receiver "mm"
                    alias = child.child_by_field_name("alias") if aliased else None
                    bindings[node_text(alias, source) if alias is not None else module] = resolved

        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name") or (
                node.named_children[0] if node.named_children else None
            )
            if module_node is None:
                continue
            level, module = 0, ""
            if module_node.type == "relative_import":
                for c in module_node.named_children:
                    if c.type == "import_prefix":
                        level = node_text(c, source).count(".")
                    elif c.type == "dotted_name":
                        module = _dotted(c, source)
            elif module_node.type == "dotted_name":
                module = _dotted(module_node, source)
            resolved = _resolve(module, level, file_path, repo_root, roots)
            (internal if resolved else external).setdefault(resolved or module or ".", None)
            if resolved:  # `from m import a, b as c` → a, c → m's file
                for item in node.named_children:
                    if item is module_node:
                        continue
                    if item.type == "dotted_name":
                        bindings[node_text(item, source).split(".")[-1]] = resolved
                    elif item.type == "aliased_import":
                        al = item.child_by_field_name("alias")
                        if al is not None:
                            bindings[node_text(al, source)] = resolved

        elif node.type == "assignment":
            target = node.named_children[0] if node.named_children else None
            if target is not None and target.type == "identifier" and node_text(target, source) == "__all__":
                for s in _iter(node):
                    if s.type == "string_content":
                        exports.append(node_text(s, source))

    return list(internal), list(external), exports, bindings
