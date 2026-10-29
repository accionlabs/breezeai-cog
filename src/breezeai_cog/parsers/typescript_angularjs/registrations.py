"""Controller / component / directive registrations, and the ``angularjs.registrations`` fact.

AngularJS names a unit with a string and registers its function on a module::

    angular.module('shop').controller('ListCtrl', function ($scope) { … })   // inline
    var app = angular.module('shop');
    app.controller('DetailCtrl', DetailCtrl);                                // a declared function
    app.component('cartBadge', { templateUrl: 'badge.html', controller: 'BadgeCtrl' });

A registration counts only when its receiver is proved to be a module: a chain that starts at
``angular.module(…)``, or a variable assigned from one in the same file. ``.controller(…)`` and
``.directive(…)`` also exist in other libraries (``Vue.directive``), and a bare ``app`` defined
in another file is not proof. Each registration becomes a statement carrying its ``name`` and
``uiRole``; ``.service`` / ``.factory`` are not UI units and are not recorded.

A route names its controller with the same string, usually from another file. While the
TypeScript index is built, ``_collect`` reads every file's registrations and ``_resolve`` keeps
``(kind, name)`` → where it starts for each name registered **once**: a line in the registering
file, or — for a function or object imported from another module, the usual ES-module layout —
the import (module, export), which the route's detector follows to the declaration the way any
TypeScript handler is found (the index has no declarations yet while facts resolve). A name
registered twice, or at a place that can't be read, is dropped, so a route never gets a guessed
location.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from ..additive import DetectContext, FactSpec, index_fact, register_fact
from ..emits import Emit
from ..handler_sites import handler_location, locate_export
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.declarations import import_bindings
from ..typescript.imports import _is_fixture_path, _resolve as _resolve_module
from ..typescript.navigation import literal
from .syntax import (
    FUNCTIONS,
    MethodCall,
    Scan,
    call_emit,
    chain_root,
    handler_of,
    scan,
    starts_at_angular_module,
    unwrap,
)

FACT = "angularjs.registrations"

#: Registration method → the ``uiRole`` of the unit it declares.
UI_ROLES = {"controller": "controller", "component": "component", "directive": "directive"}
#: The kinds a route can name, and so the kinds the fact holds.
LOOKED_UP = frozenset({"controller", "component"})

#: ``(kind, name)`` and the file and line where the registered function / definition starts.
Key = tuple[str, str]
Site = tuple[str, int]


class Import(NamedTuple):
    """A function or object written in another module: the registering file, the module
    specifier and the export."""

    file: str
    spec: str
    export: str


Locate = Callable[[Key], Site | None]
#: A controller written as a variable → ``(handler, handlerLine, handlerPath)``; ``handler`` is
#: the declared name, which an import may rename.
Variable = Callable[[str], tuple[str, int | None, str | None]]


@dataclass(frozen=True, slots=True)
class Env:
    """What reading a route or state config needs: the file's source and path, and the lookups
    for a registered name and for a variable."""

    source: bytes
    path: str
    locate: Locate
    variable: Variable


@dataclass(frozen=True, slots=True)
class Registration:
    call: MethodCall
    kind: str
    name: str
    #: Line (in this file) where the registered function or definition starts; ``None`` when it
    #: is not written here.
    line: int | None
    #: ``(module specifier, export)`` when the function or object is imported.
    imported: tuple[str, str] | None = None


def _start_line(arg: Node, kind: str, found: Scan, source: bytes) -> int | None:
    """Where the registered thing starts: an inline function (also the last element of an
    annotated array), a function declared in this file, or a component's definition object."""
    arg = unwrap(arg)
    if arg.type in FUNCTIONS:
        return arg.start_point[0] + 1
    if arg.type == "identifier":  # a function, class or object declared in this file
        return found.declared.get(node_text(arg, source))
    if arg.type == "object" and kind == "component":
        return arg.start_point[0] + 1
    return None


def _identifier(arg: Node, source: bytes) -> str | None:
    """The variable a registration passes (also the last element of an annotated array)."""
    arg = unwrap(arg)
    return node_text(arg, source) if arg.type == "identifier" else None


def registrations(found: Scan, source: bytes, root: Node) -> list[Registration]:
    out: list[Registration] = []
    imports: dict[str, tuple[str, str]] | None = None
    for mc in found.calls:
        kind = mc.name(source)
        if kind not in UI_ROLES or len(mc.arguments) < 2:
            continue
        name = literal(mc.arguments[0], source)
        origin = chain_root(mc.receiver)
        on_module = starts_at_angular_module(mc.receiver, source) or (
            origin.type == "identifier" and node_text(origin, source) in found.modules
        )
        if not (name and on_module):
            continue
        arg = mc.arguments[1]
        line = _start_line(arg, kind, found, source) if kind in LOOKED_UP else None
        imported = None
        variable = _identifier(arg, source) if line is None and kind in LOOKED_UP else None
        if variable is not None:
            imports = import_bindings(root, source) if imports is None else imports
            bound = imports.get(variable)
            imported = bound if bound is not None and bound[1] != "*" else None
        out.append(Registration(mc, kind, name, line, imported))
    return out


def registration_emits(found: list[Registration], source: bytes) -> list[Emit]:
    return [call_emit(r.call, source, {"name": r.name, "uiRole": UI_ROLES[r.kind]}) for r in found]


def _sites(found: list[Registration], rel: str) -> list[tuple[Key, Site | Import | None]]:
    out: list[tuple[Key, Site | Import | None]] = []
    for r in found:
        if r.kind not in LOOKED_UP:
            continue
        where: Site | Import | None = None
        if r.line is not None:
            where = (rel, r.line)
        elif r.imported is not None:
            where = Import(rel, *r.imported)
        out.append(((r.kind, r.name), where))
    return out


def _collect(
    root: Node,
    source: bytes,
    rel: str,
    repo_root: Path | None,
) -> list[tuple[Key, Site | Import | None]] | None:
    if _is_fixture_path(rel):
        return None
    return _sites(registrations(scan(root, source), source, root), rel) or None


def _resolve(
    values: list[list[tuple[Key, Site | Import | None]]], index: object
) -> dict[Key, Site | Import]:
    sites: dict[Key, Site | Import | None] = {}
    for per_file in values:
        for key, where in per_file:
            record_distinct(sites, key, where)
    return {key: where for key, where in sites.items() if where is not None}


register_fact(
    FactSpec(
        name=FACT,
        language="typescript",
        gate=(b".module(",),
        collect=_collect,
        resolve=_resolve,
    )
)


def locator(dc: DetectContext, here: list[Registration]) -> Locate:
    """Where a registered name starts: the repository's fact, or — for a single file parsed
    without the repository index — this file's own registrations. An import is followed to the
    declaration it names; one that can't be followed gives no location."""
    index = dc.ctx.resolution_index
    sites = index_fact(index, FACT)
    if sites is None:
        sites = _resolve([_sites(here, dc.path)], None)

    def locate(key: Key) -> Site | None:
        where = sites.get(key)
        if not isinstance(where, Import):
            return where
        module = _resolve_module(where.spec, where.file, dc.ctx.repo_root, index)
        decl = locate_export(index, module, where.export) if module else None
        return (decl.file, decl.line) if decl is not None else None

    return locate


def handler_fields(view: dict[str, Node], env: Env) -> dict[str, object]:
    """``handler`` / ``handlerLine`` / ``handlerPath`` of a view config (an ngRoute route or a
    ui-router state) from its ``controller`` or ``component``. A registered name is looked up in
    the registrations; a variable is looked up as any TypeScript handler is. Nothing found leaves
    the location empty."""
    key = "controller" if "controller" in view else "component" if "component" in view else None
    if key is None:
        return {}
    handler = handler_of(view[key], env.source)
    name, line, handler_path = handler.name, handler.line, None
    if name is not None and handler.registered:
        line, handler_path = handler_location(env.locate((key, name)), env.path)
    elif name is not None:
        name, line, handler_path = env.variable(name)
    return {"handler": name, "handlerLine": line, "handlerPath": handler_path}
