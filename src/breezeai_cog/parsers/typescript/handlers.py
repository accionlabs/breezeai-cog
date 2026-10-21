"""Lead a TypeScript / JavaScript entry point to its handler's declaration: ``handlerLine``, and
``handlerPath`` when it is another file (contract §5).

Detectors name the handler as the code writes it — a component, an imported function, a lazy
import. :func:`link_handlers` runs once per file after them and looks the name up:

==========================================  ==================================================
The handler is …                            Found through
==========================================  ==================================================
declared in this file                       its line here (no ``handlerPath``)
imported (``import X from './x'``)          the module's export ``X`` / ``default``
a lazy wrapper (``const X = lazy(() =>``    the imported module's ``default`` export, or the
``import('./x'))``)                          ``.then(m => m.Y)`` member
loaded lazily (``import('./x').then(m =>``   the ``.then`` member ``X`` of that module
``m.X)``)
an import path (``'./home/home.component'``)  the module's ``default`` export; ``handler``
                                            becomes the declared name, or empty when the
                                            module can't be found — never a path
==========================================  ==================================================

A module's exports come from the ``typescript.declarations`` fact (re-exports followed). Not
found, or ambiguous → the record is left as it is (honest-null).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord, Statement
from ..treesitter import node_text
from .declarations import import_bindings, resolve_export, top_level_declarations

#: Statements that point at a handler.
_HANDLED = frozenset({"route", "eventbus_consumer", "timer"})
# `import('./x').then(m => m.X)` / `.then((m) => m.X)`.
_LAZY_THEN_RE = re.compile(
    r"""import\(\s*['"]([^'"]+)['"]\s*\)\s*\.then\(\s*\(?\s*([\w$]+)\s*\)?\s*=>\s*\2\.([\w$]+)""")
_LAZY_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
# A handler written as a module specifier: './x', '../x', '@/x', '~/x', 'pkg/sub'.
_SPECIFIER_RE = re.compile(r"^(?:\.{1,2}/|[@~]/|@?[\w.-]+/)[\w@./$-]*$")


def needs_location(record: FileRecord) -> bool:
    return any(_unlocated(s) for s in record.statements)


def _unlocated(s: Statement) -> bool:
    return (s.semanticType in _HANDLED and bool(s.handler) and s.handlerLine is None
            and s.handlerPath is None)


def _lazy_target(text: str) -> tuple[str, str] | None:
    """(module, export) a lazy-import expression loads, or None."""
    m = _LAZY_THEN_RE.search(text)
    if m:
        return m.group(1), m.group(3)
    m = _LAZY_RE.search(text)
    return (m.group(1), "default") if m else None


def link_handlers(record: FileRecord, root: Node, source: bytes, path: str,
                  repo_root: Path | None, index: Any | None) -> None:
    """Fill ``handlerLine`` / ``handlerPath`` (and a path-shaped ``handler``) in place."""
    from .imports import _resolve

    decls = top_level_declarations(root, source)
    imports = import_bindings(root, source)
    then_members: dict[str, str | None] = {}  # `.then(m => m.X)` member → its module
    for m in _LAZY_THEN_RE.finditer(source.decode("utf-8", "replace")):
        prev = then_members.get(m.group(3), m.group(1))
        then_members[m.group(3)] = m.group(1) if prev == m.group(1) else None

    for s in record.statements:
        if not _unlocated(s):
            continue
        name = s.handler or ""
        target: tuple[str, str] | None = None
        path_shaped = bool(_SPECIFIER_RE.match(name))
        if path_shaped:
            target = (name, "default")
        elif name in decls:
            line, value = decls[name]
            lazy = _lazy_target(node_text(value, source)) if "import(" in node_text(
                value, source) else None
            if lazy is None:
                s.handlerLine = line  # declared in this file
                continue
            target = lazy
        elif name in imports and imports[name][1] != "*":
            target = imports[name]
        elif then_members.get(name):
            target = (then_members[name] or "", name)
        if target is None or repo_root is None:
            continue
        module_file = _resolve(target[0], path, repo_root, index)
        if module_file is None and path_shaped and not name.startswith("."):
            # React Router v7 `route('about', 'routes/about.tsx')`: relative to this folder.
            module_file = _resolve("./" + name, path, repo_root, index)
        decl = resolve_export(index, module_file, target[1]) if module_file else None
        if decl is None:
            if path_shaped:
                s.handler = None  # a path is not a name: unknown, never the path (§5)
            continue
        if decl.name:
            s.handler = decl.name
        elif path_shaped:
            s.handler = None  # an anonymous default export: no name, never the path
        s.handlerLine = decl.line
        if decl.file != path:
            s.handlerPath = decl.file
