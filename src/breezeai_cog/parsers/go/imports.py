"""Go import extraction + repo-level resolver index."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from tree_sitter import Node

from ..additive import (
    collect_additive, collect_fact_sources, facts_for, resolve_additive, split_fact_sources,
)
from ..index_common import parallel_map
from ..treesitter import node_text, parse_source


@dataclass(frozen=True)
class GoIndex:
    #: Package import path -> a repo-relative file of that package.
    modules: dict[str, str | None] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name -> fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)

    def package_dir(self, import_path: str) -> str | None:
        """Repo-relative directory of an in-repo package (``""`` = the repo root), or None."""
        target = self.modules.get(import_path)
        return target.rpartition("/")[0] if target is not None else None


def _registered(args: tuple[Path, Path]) -> dict[str, Any]:
    """Picklable per-file worker: what registered facts collect from one file. Only files
    matching a fact's gate are parsed."""
    repo_root, abs_path = args
    try:
        source = abs_path.read_bytes()
    except OSError:
        return {}
    if not any(g in source for spec in facts_for("go") for g in spec.gate):
        return {}
    try:
        root = parse_source("go", source).root_node
    except ValueError:  # bounded-parse failure — skip this file only
        return {}
    return collect_additive("go", root, source, abs_path.relative_to(repo_root).as_posix(),
                            repo_root)


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
    index = _module_index(Path(repo_root), files)
    files, fact_files = split_fact_sources("go", list(files))
    go_files = [Path(f) for f in files if Path(f).suffix == ".go"]
    collected: dict[str, list[Any]] = collect_fact_sources("go", fact_files, Path(repo_root), jobs)
    if facts_for("go"):
        for per_file in parallel_map([(Path(repo_root), f) for f in go_files], _registered, jobs):
            for name, value in per_file.items():
                collected.setdefault(name, []).append(value)
    # Registered facts may need the package map, so they resolve after it is built.
    return replace(index, facts=resolve_additive("go", collected, index))


def _module_index(repo_root: Path, files: Sequence[Path]) -> GoIndex:
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


def default_package_name(import_path: str) -> str:
    """The name a package is used by when imported without an alias, by the rule ``goimports``
    assumes (``ImportPathToAssumedName``): the last path segment, the one before it when that is a
    major version (``…/echo/v4`` → ``echo``), without a ``go-`` prefix, up to the first character
    that can't be in an identifier (``nats.go`` → ``nats``, ``kafka-go`` → ``kafka``,
    ``yaml.v3`` → ``yaml``)."""
    parts = import_path.split("/")
    base = parts[-1]
    if len(parts) > 1 and base[:1] == "v" and base[1:].isdigit():
        base = parts[-2]
    if base.startswith("go-"):
        base = base[3:]
    for i, ch in enumerate(base):
        if not (ch.isalnum() or ch == "_"):
            return base[:i]
    return base


def _import_specs(root: Node) -> list[Node]:
    """Every ``import_spec`` of the file — single (``import "x"``) and grouped (``import ( … )``)."""
    specs: list[Node] = []
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        for item in child.named_children:
            if item.type == "import_spec":
                specs.append(item)
            elif item.type == "import_spec_list":
                specs.extend(c for c in item.named_children if c.type == "import_spec")
    return specs


def import_aliases(root: Node, source: bytes) -> dict[str, str]:
    """Every name a file refers to an imported package by → its import path (blank ``_`` and
    dot ``.`` imports are skipped)."""
    out: dict[str, str] = {}
    for spec in _import_specs(root):
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            continue
        module = node_text(path_node, source).strip('"`')
        alias_node = spec.child_by_field_name("name")
        alias = node_text(alias_node, source) if alias_node is not None else default_package_name(module)
        if alias not in {"_", "."}:
            out[alias] = module
    return out


def extract_imports(
    root: Node,
    source: bytes,
    index: GoIndex | None = None,
) -> tuple[list[str], dict[str, str]]:
    """(externalImports, alias → package dir of each in-repo import). An in-repo package is a
    directory, so its import is not one file: the parser turns the names the file uses from it
    into ``importFiles`` (see ``GoParser.extract``)."""
    external: set[str] = set()
    packages: dict[str, str] = {}
    for spec in _import_specs(root):
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            continue
        module = node_text(path_node, source).strip('"`')
        pkg_dir = index.package_dir(module) if index is not None else None
        if pkg_dir is None:
            external.add(module)
            continue
        alias_node = spec.child_by_field_name("name")
        alias = node_text(alias_node, source) if alias_node is not None else default_package_name(module)
        if alias not in {"_", "."}:
            packages[alias] = pkg_dir
    return sorted(external), packages
