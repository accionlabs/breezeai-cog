"""Syntax helpers shared by the Python framework parsers (FastAPI, Flask, Django / DRF).

Reading a literal, a keyword argument or a module-level constant, and following an import to the
repository file it names, are the same for every Python framework. They live here so no
framework package imports another's (architecture §15).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from tree_sitter import Node

from .treesitter import node_text


def field_text(node: Node | None, field: str, source: bytes) -> str:
    """The text of ``node``'s child ``field`` (a definition's ``name``), ``""`` when absent."""
    child = node.child_by_field_name(field) if node is not None else None
    return node_text(child, source) if child is not None else ""


def receiver(call: Node) -> Node | None:
    """``x`` of a method call ``x.m(…)``; None for a plain call."""
    fn = call.child_by_field_name("function")
    return fn.child_by_field_name("object") if fn is not None and fn.type == "attribute" else None


def literal(node: Node | None, source: bytes) -> str | None:
    """A plain string literal's value (no f-string interpolation), else None."""
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    content = next((c for c in node.named_children if c.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def string_list(node: Node | None, source: bytes) -> list[str] | None:
    """The values of a list / tuple / set of plain string literals; None when the node is not
    one, or when any element is not a plain literal (the value is then not known)."""
    if node is None or node.type not in ("list", "tuple", "set"):
        return None
    out: list[str] = []
    for el in node.named_children:
        value = literal(el, source)
        if value is None:
            return None
        out.append(value)
    return out


def keyword(call: Node, source: bytes, name: str) -> Node | None:
    """The value node of keyword argument ``name`` of ``call``, or None when absent."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        key = arg.child_by_field_name("name") if arg.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == name:
            return arg.child_by_field_name("value")
    return None


def positional(call: Node) -> list[Node]:
    """The positional arguments of ``call`` (keyword arguments, ``*a`` and ``**k`` left out)."""
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "argument_list":
        return []
    return [a for a in args.named_children
            if a.type not in ("keyword_argument", "list_splat", "dictionary_splat", "comment")]


def argument(call: Node, source: bytes, index: int, name: str) -> Node | None:
    """The argument passed as keyword ``name``, else the positional one at ``index``."""
    value = keyword(call, source, name)
    if value is not None:
        return value
    args = positional(call)
    return args[index] if index < len(args) else None


def calls(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "call":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def top_statements(root: Node) -> Iterator[Node]:
    """The module's top-level statements, an ``expression_statement`` unwrapped to its
    expression (``x = 1`` is an ``assignment`` either way)."""
    for stmt in root.named_children:
        if stmt.type == "expression_statement" and stmt.named_children:
            yield stmt.named_children[0]
        else:
            yield stmt


def module_constants(root: Node, source: bytes) -> dict[str, str]:
    """``NAME = "literal"`` at module level. A name assigned more than once is left out."""
    out: dict[str, str] = {}
    twice: set[str] = set()
    for node in top_statements(root):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        name = node_text(left, source)
        value = literal(node.child_by_field_name("right"), source)
        if name in out or name in twice or value is None:
            out.pop(name, None)
            twice.add(name)
            continue
        out[name] = value
    return out


def module_file(repo_root: Path, importer: str, module: str, level: int) -> str | None:
    """The repo file a module name refers to (``pkg/mod.py`` or ``pkg/mod/__init__.py``), from
    the importing file ``importer`` (repo-relative). A relative import (``level`` dots) starts at
    the importer's package; an absolute one is tried from the importer's directory up to the repo
    root (nearest first), so ``from app.x import y`` works in a repo that holds the project in a
    subdirectory. Not a repo file (a library) → None.

    Plain string and ``os.path`` work, not ``Path`` objects: this runs for every import of every
    candidate view or router class, so the object churn showed up in the index build time."""
    parts = [p for p in module.split(".") if p]
    here = importer.split("/")[:-1]  # the importer's directory, as path parts
    starts: Sequence[int]
    if level > 0:
        up = level - 1
        starts = [len(here) - up] if up <= len(here) else []
    else:
        starts = range(len(here), -1, -1)  # the directory itself, then each one above it
    root = os.fspath(repo_root)
    tail = "/".join(parts)
    for n in starts:
        base = "/".join(here[:n])
        stem = f"{base}/{tail}" if base else tail
        cands: tuple[str, ...]
        if not parts:
            cands = (f"{base}/__init__.py" if base else "__init__.py",)
        else:
            cands = (f"{stem}.py", f"{stem}/__init__.py")
        for cand in cands:
            if os.path.isfile(f"{root}/{cand}"):
                return cand
    return None


def _import_nodes(root: Node, anywhere: bool) -> Iterator[Node]:
    if not anywhere:
        yield from root.named_children
        return
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("import_from_statement", "import_statement"):
            yield node
        else:
            stack.extend(reversed(node.named_children))


#: One import of a local name: ``("from", module, level, symbol)`` or ``("import", dotted)``.
_Entry = tuple[str, ...]


def _import_entries(root: Node, source: bytes, anywhere: bool,
                    plain: bool = False) -> dict[str, list[_Entry]]:
    """Local name → its imports in source order, as written (nothing resolved yet). ``plain``: a
    ``import pkg.mod`` also binds ``pkg`` to the package it names, which is what a dotted chain
    (``pkg.mod.urls``) starts from; off by default, so a caller that never follows a chain sees
    the same names as before."""
    out: dict[str, list[_Entry]] = {}
    for node in _import_nodes(root, anywhere):
        if node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            if mod is None:
                continue
            if mod.type == "relative_import":
                prefix = next((c for c in mod.named_children if c.type == "import_prefix"), None)
                level = len(node_text(prefix, source)) if prefix is not None else 0
                dotted = next((c for c in mod.named_children if c.type == "dotted_name"), None)
                module = node_text(dotted, source) if dotted is not None else ""
            else:
                level, module = 0, node_text(mod, source)
            for name_node in node.named_children:
                if name_node == mod or name_node.type not in ("dotted_name", "aliased_import"):
                    continue
                orig = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                alias = name_node.child_by_field_name("alias") if name_node.type == "aliased_import" else name_node
                if orig is not None and alias is not None:
                    out.setdefault(node_text(alias, source), []).append(
                        ("from", module, str(level), node_text(orig, source)))
        elif node.type == "import_statement":
            for name_node in node.named_children:
                if name_node.type == "dotted_name" and plain:  # `import pkg.mod` binds `pkg`
                    top = node_text(name_node, source).split(".", 1)[0]
                    out.setdefault(top, []).append(("import", top))
                    continue
                if name_node.type != "aliased_import":
                    continue  # without ``plain``: `import pkg.mod` binds `pkg`, not the module
                orig, alias = name_node.child_by_field_name("name"), name_node.child_by_field_name("alias")
                if orig is not None and alias is not None:
                    out.setdefault(node_text(alias, source), []).append(("import", node_text(orig, source)))
    return out


def _resolve_entry(entry: _Entry, repo_root: Path, rel: str) -> tuple[str, ...] | None:
    if entry[0] == "import":
        target = module_file(repo_root, rel, entry[1], 0)
        return ("module", target) if target is not None else None
    _, module, level, symbol = entry
    sub = module_file(repo_root, rel, f"{module}.{symbol}" if module else symbol, int(level))
    if sub is not None:  # from pkg import mod — a submodule
        return ("module", sub)
    owner = module_file(repo_root, rel, module, int(level))
    return ("symbol", owner, symbol) if owner is not None else None  # from pkg.mod import router


class ImportRefs:
    """:func:`import_refs`, one name at a time: a name is resolved (which checks the
    filesystem) only when it is asked for."""

    def __init__(self, root: Node, source: bytes, repo_root: Path | None, rel: str, *,
                 anywhere: bool = False, plain: bool = False) -> None:
        self._entries = _import_entries(root, source, anywhere, plain)
        self._repo_root, self._rel, self._anywhere = repo_root, rel, anywhere
        self._done: dict[str, tuple[str, ...] | None] = {}

    def get(self, name: str) -> tuple[str, ...] | None:
        if name in self._done:
            return self._done[name]
        seen: dict[str, tuple[str, ...] | None] = {}
        if self._repo_root is not None:
            for entry in self._entries.get(name, []):
                ref = _resolve_entry(entry, self._repo_root, self._rel)
                if ref is not None:
                    _bind(seen, name, ref, self._anywhere)
        self._done[name] = seen.get(name)
        return self._done[name]

    def all(self) -> dict[str, tuple[str, ...]]:
        return {name: ref for name in self._entries if (ref := self.get(name)) is not None}


def import_refs(root: Node, source: bytes, repo_root: Path, rel: str, *,
                anywhere: bool = False, plain: bool = False) -> dict[str, tuple[str, ...]]:
    """Local name → what it refers to in the repo: ``("module", file)`` or
    ``("symbol", file, name)``. Library imports are left out.

    Module-level imports only, unless ``anywhere``: then an import inside a function counts too
    (``def create_app(): from app.auth import bp``), and a name imported as two different things
    in two places is left out."""
    return ImportRefs(root, source, repo_root, rel, anywhere=anywhere, plain=plain).all()


def submodule(repo_root: Path | None, package_file: str, name: str) -> str | None:
    """The repo file of the submodule ``name`` of the package whose ``__init__.py`` is
    ``package_file`` (``pkg/name.py`` or ``pkg/name/__init__.py``); None when that file is not a
    package or has no such submodule."""
    folder, _, file = package_file.rpartition("/")
    if repo_root is None or file != "__init__.py":
        return None
    root = os.fspath(repo_root)
    base = f"{folder}/{name}" if folder else name
    for cand in (f"{base}.py", f"{base}/__init__.py"):
        if os.path.isfile(f"{root}/{cand}"):
            return cand
    return None


def _module_of(node: Node, source: bytes, imports: Any, repo_root: Path | None) -> str | None:
    """The repo file of the module a dotted name denotes: an imported module (``user``) or a
    submodule reached through imported packages (``user.views``)."""
    if node.type == "identifier":
        ref = imports.get(node_text(node, source))
        return ref[1] if ref is not None and ref[0] == "module" else None
    if node.type != "attribute":
        return None
    obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
    owner = _module_of(obj, source, imports, repo_root) if obj is not None else None
    return submodule(repo_root, owner, node_text(attr, source)) if owner is not None and attr is not None else None


def attribute_owner(node: Node, source: bytes, imports: Any, repo_root: Path | None
                    ) -> tuple[str, str] | None:
    """(repo file, name) of ``a.b.c`` when ``a.b`` is a module of the repo reached through
    imports: ``a`` imported as a module, each further part a submodule of the one before. None
    for anything else — a name that is not an import, a part that is not a module of the repo."""
    if node.type != "attribute":
        return None
    obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
    owner = _module_of(obj, source, imports, repo_root) if obj is not None else None
    return (owner, node_text(attr, source)) if owner is not None and attr is not None else None


def _bind(seen: dict[str, tuple[str, ...] | None], name: str, ref: tuple[str, ...],
          strict: bool) -> None:
    """Bind ``name``. ``strict``: a name bound to two different targets is ambiguous (None);
    otherwise the later import wins, as at module level."""
    seen[name] = ref if not strict or seen.get(name, ref) == ref else None


class Origins:
    """Where each imported name of one file comes from, as a dotted path — library and repo
    imports alike — so a name can be checked against the module that defines it::

        from django.contrib.auth.decorators import login_required as lr   # lr → django.contrib.auth.decorators.login_required
        from rest_framework import permissions                            # permissions → rest_framework.permissions
        import flask_login                                                # flask_login → flask_login

    A relative import's origin starts with ``.`` so it never matches a library name.
    ``star`` holds the modules imported with ``from m import *``."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.names: dict[str, str] = {}
        self.star: set[str] = set()
        for node in root.named_children:
            if node.type == "import_from_statement":
                mod = node.child_by_field_name("module_name")
                if mod is None:
                    continue
                module = node_text(mod, source)
                if any(c.type == "wildcard_import" for c in node.named_children):
                    self.star.add(module)
                for name_node in node.named_children:
                    if name_node == mod or name_node.type not in ("dotted_name", "aliased_import"):
                        continue
                    orig = name_node.child_by_field_name("name") if name_node.type == "aliased_import" else name_node
                    alias = name_node.child_by_field_name("alias") if name_node.type == "aliased_import" else name_node
                    if orig is not None and alias is not None:
                        sep = "" if module.endswith(".") else "."
                        self.names[node_text(alias, source)] = f"{module}{sep}{node_text(orig, source)}"
            elif node.type == "import_statement":
                for name_node in node.named_children:
                    if name_node.type == "aliased_import":
                        orig, alias = name_node.child_by_field_name("name"), name_node.child_by_field_name("alias")
                        if orig is not None and alias is not None:
                            self.names[node_text(alias, source)] = node_text(orig, source)
                    elif name_node.type == "dotted_name":
                        top = node_text(name_node, source).split(".", 1)[0]
                        self.names.setdefault(top, top)

    def qualified(self, written: str) -> str | None:
        """The dotted origin of a name as written (``permissions.IsAuthenticated`` →
        ``rest_framework.permissions.IsAuthenticated``); None when its first part is not imported
        (a name of this file, or unknown)."""
        head, _, rest = written.partition(".")
        origin = self.names.get(head)
        if origin is None:
            return None
        return f"{origin}.{rest}" if rest else origin

    def imports_package(self, package: str) -> bool:
        """Whether the file imports ``package`` or one of its modules."""
        return any(o == package or o.startswith(package + ".")
                   for o in (*self.names.values(), *self.star))
