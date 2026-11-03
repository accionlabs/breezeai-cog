"""Lead a TypeScript / JavaScript entry point to its handler's declaration: ``handlerLine``, and
``handlerPath`` when it is another file (contract §5).

Detectors name the handler as the code writes it — a component, an imported function, a lazy
import. :func:`handler_locator` is this language's lookup for the parser's ``link_handlers``, which
runs once per file after them:

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
required (``const { f } = require('./x')``)  the module's export ``f`` (CommonJS included)
a member of a module (``ns.f`` —            the module's export ``f``
``import * as ns`` / ``ns = require(…)``)
a static method (``Class.m``)               the class's declaration (here, or imported), its
                                            ``static m``
an instance's method (``obj.m`` —           the class's declaration, its non-static ``m``
top-level ``const obj = new Class()``)
==========================================  ==================================================

A member handler keeps the name it is written with (``users.list``); only its location is
filled.

A module's exports come from the ``typescript.declarations`` fact (re-exports followed). Not
found, or ambiguous → the record is left as it is (honest-null).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..base import HandlerLocator
from ..handler_sites import locate_export
from ..treesitter import node_text
from .declarations import (
    Declaration,
    class_member,
    class_methods,
    import_bindings,
    top_level_declarations,
)

# `import('./x').then(m => m.X)` / `.then((m) => m.X)`.
_LAZY_THEN_RE = re.compile(
    r"""import\(\s*['"]([^'"]+)['"]\s*\)\s*\.then\(\s*\(?\s*([\w$]+)\s*\)?\s*=>\s*\2\.([\w$]+)""")
_LAZY_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
# A handler written as a module specifier: './x', '../x', '@/x', '~/x', 'pkg/sub'.
_SPECIFIER_RE = re.compile(r"^(?:\.{1,2}/|[@~]/|@?[\w.-]+/)[\w@./$-]*$")


def _lazy_target(text: str) -> tuple[str, str] | None:
    """(module, export) a lazy-import expression loads, or None."""
    m = _LAZY_THEN_RE.search(text)
    if m:
        return m.group(1), m.group(3)
    m = _LAZY_RE.search(text)
    return (m.group(1), "default") if m else None


def _member_target(
    obj: str, member: str, decls: dict[str, tuple[int, Node]],
    imports: dict[str, tuple[str, str]], modules: set[str], source: bytes, path: str,
    repo_root: Path, index: Any | None,
) -> Declaration | None:
    """The declaration of handler ``obj.member``: an export of the module ``obj`` names, or a
    method of the class ``obj`` is (static) or is an instance of (``obj = new Class()``)."""
    from .imports import _resolve

    def module_of(name: str) -> str | None:
        return _resolve(imports[name][0], path, repo_root, index) if name in imports else None

    def class_decl(name: str) -> tuple[Declaration, Node | None] | None:
        """The class ``name`` refers to here: (its declaration, its node when it is in this
        file)."""
        if name in decls:
            line, node = decls[name]
            if node.type in ("class_declaration", "abstract_class_declaration", "class"):
                return Declaration(name, path, line), node
            return None
        module = module_of(name)
        decl = locate_export(index, module, imports[name][1]) if module is not None else None
        return (decl, None) if decl is not None and decl.name else None

    if obj in modules:
        module = module_of(obj)  # import * as ns / const ns = require('./x')
        return locate_export(index, module, member) if module is not None else None
    static = True
    if obj in decls and decls[obj][1].type == "new_expression":
        ctor = decls[obj][1].child_by_field_name("constructor")
        if ctor is None or ctor.type != "identifier":
            return None
        obj, static = node_text(ctor, source), False
    found = class_decl(obj)
    if found is None:
        return None
    decl, node = found
    if node is not None:  # a class of this file
        hit = class_methods(node, source).get(member)
        return Declaration(member, path, hit[0]) if hit is not None and hit[1] == static else None
    return class_member(index, decl, member, static)


#: Nodes that open a scope a ``const`` / ``let`` / parameter can shadow a top-level name in: a
#: function, a block, a loop (``for (let x …)``), a ``switch`` body and a ``catch`` clause.
_SCOPES = frozenset({"function_declaration", "generator_function_declaration",
                     "function_expression", "function", "generator_function", "arrow_function",
                     "method_definition", "statement_block", "for_statement", "for_in_statement",
                     "switch_body", "catch_clause"})
#: Declarations that bind their name in the enclosing scope.
_NAMED_DECLS = frozenset({"function_declaration", "generator_function_declaration",
                          "class_declaration", "abstract_class_declaration"})
#: A scope's own bindings: a function's parameters, ``catch (e)``, ``for (const x of …)``.
_SCOPE_BINDINGS = ("parameters", "parameter", "left")


def _pattern_names(node: Node | None, source: bytes) -> set[str]:
    """The names a binding pattern binds: ``x``, ``{ a, b: c }``, ``[d, ...e]``, ``f = 1``."""
    if node is None:
        return set()
    if node.type in ("identifier", "shorthand_property_identifier_pattern"):
        return {node_text(node, source)}
    if node.type in ("assignment_pattern", "object_assignment_pattern"):
        return _pattern_names(node.child_by_field_name("left") or node.named_children[0], source)
    if node.type == "pair_pattern":
        return _pattern_names(node.child_by_field_name("value"), source)
    if node.type in ("required_parameter", "optional_parameter"):
        return _pattern_names(node.child_by_field_name("pattern"), source)
    out: set[str] = set()
    if node.type in ("object_pattern", "array_pattern", "rest_pattern", "formal_parameters"):
        for c in node.named_children:
            out |= _pattern_names(c, source)
    return out


def _local_scopes(root: Node, source: bytes) -> list[tuple[int, int, frozenset[str]]]:
    """Every function / block / loop / ``catch`` below the top level → (first line, last line,
    the names declared directly in it). Top-level names are not in it: they are what a local
    shadows. Walked with an explicit stack (a deep expression must not exhaust recursion)."""
    scopes: list[tuple[Node, set[str]]] = []
    stack: list[tuple[Node, set[str] | None]] = [(root, None)]
    while stack:
        node, names = stack.pop()
        own = names
        if node.type in _SCOPES:
            own = set()
            scopes.append((node, own))
            for field in _SCOPE_BINDINGS:
                bound = node.child_by_field_name(field)
                if bound is not None and (field != "left" or node.type == "for_in_statement"):
                    own |= _pattern_names(bound, source)
        if names is not None:  # the top level (``names is None``) is not a local scope
            if node.type == "variable_declarator":
                names |= _pattern_names(node.child_by_field_name("name"), source)
            elif node.type in _NAMED_DECLS:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
        stack.extend((c, own) for c in node.named_children)
    return [(n.start_point[0] + 1, n.end_point[0] + 1, frozenset(names))
            for n, names in scopes if names]


def handler_locator(root: Node, source: bytes, path: str, repo_root: Path | None,
                    index: Any | None) -> HandlerLocator:
    """This file's lookup for :meth:`~..base.BaseParser.link_handlers`."""
    from .imports import _resolve, relative_requires

    decls = top_level_declarations(root, source)
    imports = import_bindings(root, source)
    requires = relative_requires(root, source)
    for local, binding in requires.items():
        imports.setdefault(local, binding)
    # Names bound to a whole module — `import * as ns` / `const ns = require('./x')` — whose
    # members are the module's exports.
    modules = {n for n, (_, bound) in imports.items() if bound == "*"} | {
        n for n, (_, bound) in requires.items()
        if bound == "default" and imports.get(n, ("", ""))[1] == "default"}
    then_members: dict[str, str | None] = {}  # `.then(m => m.X)` member → its module
    for m in _LAZY_THEN_RE.finditer(source.decode("utf-8", "replace")):
        prev = then_members.get(m.group(3), m.group(1))
        then_members[m.group(3)] = m.group(1) if prev == m.group(1) else None

    scopes: list[tuple[int, int, frozenset[str]]] | None = None

    def shadowed(name: str, line: int) -> bool:
        """Whether a function or block around ``line`` declares ``name`` again: the record then
        names that local, not the top-level declaration or import."""
        nonlocal scopes
        if scopes is None:
            scopes = _local_scopes(root, source)
        return any(start <= line <= end and name in names for start, end, names in scopes)

    def locate(name: str, line: int | None = None) -> tuple[str | None, tuple[str, int] | None] | None:
        target: tuple[str, str] | None = None
        path_shaped = bool(_SPECIFIER_RE.match(name))
        if not path_shaped and line is not None and shadowed(name.split(".", 1)[0], line):
            return None  # a local of the same name: where it is declared is not looked up
        if path_shaped:
            target = (name, "default")
        elif name in decls:
            line, value = decls[name]
            lazy = _lazy_target(node_text(value, source)) if "import(" in node_text(
                value, source) else None
            if lazy is None:
                return name, (path, line)  # declared in this file
            target = lazy
        elif name in imports and imports[name][1] != "*":
            target = imports[name]
        elif then_members.get(name):
            target = (then_members[name] or "", name)
        elif name.count(".") == 1 and repo_root is not None:
            obj, member = name.split(".")
            found = _member_target(obj, member, decls, imports, modules, source, path,
                                   repo_root, index)
            # A member handler keeps the name it is written with; only its site is found.
            return (name, (found.file, found.line)) if found is not None else None
        if target is None or repo_root is None:
            return None
        module_file = _resolve(target[0], path, repo_root, index)
        if module_file is None and path_shaped and not name.startswith("."):
            # React Router v7 `route('about', 'routes/about.tsx')`: relative to this folder.
            module_file = _resolve("./" + name, path, repo_root, index)
        decl = locate_export(index, module_file, target[1]) if module_file else None
        if decl is None:
            # A path is not a name: unknown, never the path (§5).
            return (None, None) if path_shaped else None
        # An anonymous default export has no name; a path-shaped handler then has none either.
        handler = decl.name or (None if path_shaped else name)
        return handler, (decl.file, decl.line)

    return locate
