"""Scala import extraction + FQCN resolution.

Scala imports resolve to in-repo files via a repo-level FQCN index built by ``build_index``
(maps each file's ``package.TypeName`` → repo-relative path). Wildcard imports stay external.
Selector groups (``{X, Y}``) and renames (``{X => Y}``, Scala 3 ``{X as Y}``) are supported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...utils import repo_relative
from ..additive import (
    collect_additive, collect_fact_sources, resolve_additive, split_fact_sources,
)
from ..constfold import Token
from ..index_common import parallel_map, record_distinct, seed_same_package
from ..treesitter import node_text, parse_source

_TYPE_DECLS = (
    "class_definition",
    "object_definition",
    "trait_definition",
    "enum_definition",
    "package_object",
)

#: "com.acme.Foo" → repo-relative path, or ``None`` when >1 file declares the same FQCN
#: (ambiguous → honest-null). Built by :func:`build_fqcn_index`.
FqcnIndex = dict[str, str | None]


@dataclass(frozen=True)
class ScalaIndex:
    """Repo-level pre-pass result: the FQCN → path map for import resolution."""

    fqcn: FqcnIndex = field(default_factory=dict)
    consts: dict[str, str] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _package_of(root: Node, source: bytes) -> str:
    pkgs: list[str] = []
    for node in root.named_children:
        if node.type == "package_clause":
            nm = node.child_by_field_name("name")
            if nm is not None:
                txt = node_text(nm, source).strip()
                if txt:
                    pkgs.append(txt)
    return ".".join(pkgs)


@dataclass(frozen=True)
class VisibleNames:
    """External type names a file can refer to by a bare name.

    ``names`` maps a local name to the FQCN it imports (a rename binds the alias);
    ``packages`` holds every package whose members are all visible — wildcard imports
    and the file's own package clauses. Unlike :func:`extract_imports`, imports at any
    depth count (a class body may hold them), and nothing needs the repo index.
    """

    names: dict[str, str]
    packages: frozenset[str]

    def resolve(self, name: str, fqcn: str) -> bool:
        """True when bare ``name`` refers to ``fqcn`` in this file."""
        package, _, simple = fqcn.rpartition(".")
        if name in self.names:
            return self.names[name] == fqcn
        return name == simple and package in self.packages


def visible_names(root: Node, source: bytes) -> VisibleNames:
    names: dict[str, str] = {}
    packages: set[str] = set()
    # ``package a.b`` then ``package c`` makes both ``a.b`` and ``a.b.c`` visible.
    clauses = [
        node_text(name, source).strip()
        for node in root.named_children
        if node.type == "package_clause" and (name := node.child_by_field_name("name"))
    ]
    for i in range(1, len(clauses) + 1):
        packages.add(".".join(clauses[:i]))

    stack = [root]
    while stack:
        node = stack.pop()
        if node.type != "import_declaration":
            stack.extend(node.named_children)
            continue
        path = [node_text(c, source) for c in node.children_by_field_name("path")]
        path = [part for part in path if part != "."]
        last = node.named_children[-1] if node.named_children else None
        prefix = ".".join(path)
        if last is not None and last.type == "namespace_wildcard":
            packages.add(prefix)
        elif last is not None and last.type == "namespace_selectors":
            for sel in last.named_children:
                if sel.type == "namespace_wildcard":
                    packages.add(prefix)
                elif sel.type == "identifier":
                    names[node_text(sel, source)] = f"{prefix}.{node_text(sel, source)}"
                elif sel.type in ("arrow_renamed_identifier", "as_renamed_identifier"):
                    orig = sel.child_by_field_name("name")
                    alias = sel.child_by_field_name("alias")
                    if orig is not None and alias is not None:
                        names[node_text(alias, source)] = f"{prefix}.{node_text(orig, source)}"
        elif len(path) > 1:
            names[path[-1]] = prefix
    return VisibleNames(names, frozenset(packages))


def _collect_top_level_types(root: Node, source: bytes) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    package = _package_of(root, source)

    for child in root.named_children:
        if child.type in _TYPE_DECLS:
            nm = child.child_by_field_name("name")
            if nm is not None:
                results.append((package, node_text(nm, source)))
        elif child.type == "package_clause":
            sub_nm = child.child_by_field_name("name")
            sub_pkg = node_text(sub_nm, source).strip() if sub_nm is not None else ""
            for sub_child in child.named_children:
                if sub_child.type in _TYPE_DECLS:
                    nm = sub_child.child_by_field_name("name")
                    if nm is not None:
                        results.append((sub_pkg, node_text(nm, source)))

    return results


def _fqcn_index_one(
    args: tuple[str, str],
) -> tuple[dict[str, str], dict[str, list[Token]], dict[str, Any]] | None:
    file_s, rel = args
    repo_root = Path(file_s[: -len(rel) - 1]) if file_s.endswith("/" + rel) else None
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return None
    try:
        root = parse_source("scala", source, 0).root_node
        fqcn_frag: dict[str, str] = {}
        for package, name in _collect_top_level_types(root, source):
            fqcn_frag[f"{package}.{name}" if package else name] = rel
        return fqcn_frag, {}, collect_additive("scala", root, source, rel, repo_root)
    except Exception as exc:
        from ...logging import get_logger

        get_logger("breezeai_cog.index").warning(
            "index.file.skipped",
            path=file_s,
            language="scala",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None


def build_fqcn_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> ScalaIndex:
    files, fact_files = split_fact_sources("scala", list(files))
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("scala", fact_files, repo_root, jobs)
    fqcn: FqcnIndex = {}
    for frag in parallel_map(args, _fqcn_index_one, jobs):
        if frag is None:
            continue
        fqcn_frag, _, facts = frag
        for name, rel in fqcn_frag.items():
            record_distinct(fqcn, name, rel)
        for name, value in facts.items():
            collected.setdefault(name, []).append(value)
    index = ScalaIndex(fqcn=fqcn)
    # Registered facts may need the whole index, so they resolve after it is built.
    return replace(index, facts=resolve_additive("scala", collected, index))


def _resolve(fqcn: str, index: FqcnIndex | None) -> str | None:
    if index is None:
        return None
    return index.get(fqcn)


def extract_imports(
    root: Node,
    source: bytes,
    file_path: str,
    repo_root: str | Path,
    index: FqcnIndex | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    bindings: dict[str, str] = {}

    package = _package_of(root, source)

    # --- relative imports -------------------------------------------------------
    # Scala resolves an import against names already in scope, so a *package* can be
    # imported as a value and then used as a prefix:
    #
    #     import org.apache.pekko          // binds `pekko` -> org.apache.pekko
    #     import pekko.PekkoException      // relative
    #
    # Pekko does this in 2,492 of its 2,774 files, and Akka before it; taken literally
    # `pekko.PekkoException` matches nothing and the whole codebase loses its IMPORTS
    # edges. All enclosing package segments are in scope too, which covers the files
    # that rely on that instead of an explicit import.
    #
    # Safety: a wrong prefix guess cannot invent an edge — the expanded name still has
    # to hit the FQCN index, which is itself honest-null on ambiguity.
    prefixes: dict[str, str] = {}
    if package:
        parts = package.split(".")
        for i in range(1, len(parts) + 1):
            prefixes[parts[i - 1]] = ".".join(parts[:i])

    def resolve_maybe_relative(fqcn: str) -> str | None:
        """Resolve ``fqcn`` as written; failing that, expand a known package prefix."""
        direct = _resolve(fqcn, index)
        if direct is not None:
            return direct
        head, _, rest = fqcn.partition(".")
        base = prefixes.get(head)
        if base is None or not rest:
            return None
        expanded = f"{base}.{rest}"
        return _resolve(expanded, index) if expanded != fqcn else None

    for node in root.named_children:
        if node.type != "import_declaration":
            continue

        named = node.named_children
        if not named:
            continue

        last = named[-1]
        if last.type == "namespace_wildcard":
            prefix = ".".join(node_text(c, source) for c in named[:-1] if c.type == "identifier")
            external.setdefault(f"{prefix}.*" if prefix else "*", None)
        elif last.type == "namespace_selectors":
            prefix = ".".join(node_text(c, source) for c in named[:-1] if c.type == "identifier")
            for sel in last.named_children:
                if sel.type == "namespace_wildcard":
                    external.setdefault(f"{prefix}.*" if prefix else "*", None)
                elif sel.type == "identifier":
                    name = node_text(sel, source)
                    fqcn = f"{prefix}.{name}" if prefix else name
                    resolved = resolve_maybe_relative(fqcn)
                    (internal if resolved else external).setdefault(resolved or fqcn, None)
                    if resolved:
                        bindings[name] = resolved
                elif sel.type in ("arrow_renamed_identifier", "as_renamed_identifier"):
                    ids = [c for c in sel.named_children if c.type == "identifier"]
                    if len(ids) >= 2:
                        orig_name = node_text(ids[0], source)
                        alias_name = node_text(ids[1], source)
                        fqcn = f"{prefix}.{orig_name}" if prefix else orig_name
                        resolved = resolve_maybe_relative(fqcn)
                        (internal if resolved else external).setdefault(resolved or fqcn, None)
                        if resolved:
                            bindings[alias_name] = resolved
                    elif ids:
                        name = node_text(ids[0], source)
                        fqcn = f"{prefix}.{name}" if prefix else name
                        resolved = resolve_maybe_relative(fqcn)
                        (internal if resolved else external).setdefault(resolved or fqcn, None)
                        if resolved:
                            bindings[name] = resolved
        else:
            ids = [c for c in named if c.type == "identifier"]
            if ids:
                fqcn = ".".join(node_text(c, source) for c in ids)
                resolved = resolve_maybe_relative(fqcn)
                (internal if resolved else external).setdefault(resolved or fqcn, None)
                if resolved:
                    bindings[node_text(ids[-1], source)] = resolved
                else:
                    # Didn't name a type we know, so it may well name a package —
                    # register it as a prefix for the imports that follow. Scala's
                    # later-import-wins shadowing, hence plain assignment.
                    prefixes[node_text(ids[-1], source)] = fqcn

    seed_same_package(bindings, package, index)
    return list(internal), list(external), [], bindings
