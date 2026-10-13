"""NestJS address prefixes as a registered fact on the TypeScript index, ``nest.prefixes``.

Two kinds of wiring put a prefix in front of a controller's routes, usually from another file::

    // main.ts — the app's global prefix (every controller below this directory)
    app.setGlobalPrefix('api', { exclude: ['health'] });
    // app.module.ts — a RouterModule path (the controllers of the registered module)
    RouterModule.register([{ path: 'admin', module: AdminModule }]);
    @Module({ controllers: [UsersController] }) export class AdminModule {}

* ``global_prefixes``: the directory of a bootstrap file (``NestFactory.create`` /
  ``app.setGlobalPrefix``) → (prefix, exclude). prefix ``""`` = the app sets none, ``None`` = set
  but unresolvable (config / env); exclude = ``((path, METHOD | None), …)``, ``None`` when it
  cannot be read. A controller belongs to the deepest bootstrap directory above it.
* ``controller_prefixes``: controller class name → its module's composed ``RouterModule`` path
  (``None`` when ambiguous or unresolvable). Only controllers of registered modules.

Prefix pieces may name constants of other files, so they resolve against the index's constant
map once it is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import _join_segments, _path_pieces, _resolve_pieces, _string_literal

FACT = "nest.prefixes"

#: A NestJS ``setGlobalPrefix`` exclude list: ``((path, METHOD | None), …)``.
NestExclude = tuple[tuple[str, str | None], ...]
#: One file's NestJS facts: (bootstrap: (prefix pieces | ``[]`` none | None dynamic, exclude) or
#: None, @Module class → its controllers, RouterModule entries (module, parent module, pieces)).
_NestRaw = tuple[
    tuple[list[tuple[str, str]] | None, NestExclude | None] | None,
    dict[str, list[str]],
    list[tuple[str, str | None, list[tuple[str, str]] | None]],
]

_NEST_BOOT = (b"NestFactory", b"setGlobalPrefix")

@dataclass(frozen=True)
class NestPrefixes:
    global_prefixes: dict[str, tuple[str | None, NestExclude | None]]
    controller_prefixes: dict[str, str | None]


def nest_prefixes(index: Any | None) -> NestPrefixes | None:
    found: NestPrefixes | None = index_fact(index, FACT)
    return found


def _nest_exclude(opts: Node | None, source: bytes) -> NestExclude | None:
    """``{ exclude: ['health', { path: 'metrics', method: RequestMethod.GET }] }`` → entries;
    ``()`` without an exclude; None when any entry is not a literal path (cannot be decided)."""
    if opts is None:
        return ()
    if opts.type != "object":
        return None
    for pair in opts.named_children:
        key = pair.child_by_field_name("key") if pair.type == "pair" else None
        if key is None or node_text(key, source).strip("'\"") != "exclude":
            continue
        arr = pair.child_by_field_name("value")
        if arr is None or arr.type != "array":
            return None
        out: list[tuple[str, str | None]] = []
        for e in arr.named_children:
            if e.type == "string":
                out.append((_string_literal(e, source) or "", None))
                continue
            if e.type != "object":
                return None
            path_v, method = None, None
            for kv in e.named_children:
                k = kv.child_by_field_name("key") if kv.type == "pair" else None
                v = kv.child_by_field_name("value") if k is not None else None
                if k is None or v is None:
                    return None
                name = node_text(k, source).strip("'\"")
                if name == "path":
                    path_v = _string_literal(v, source)
                elif name == "method":
                    method = node_text(v, source).rsplit(".", 1)[-1].upper()
            if path_v is None:
                return None
            out.append((path_v, None if method in (None, "ALL") else method))
        return tuple(out)
    return ()


def _collect_nest(root: Node, source: bytes) -> _NestRaw | None:
    """This file's NestJS wiring: a bootstrap (``NestFactory.create`` and/or
    ``app.setGlobalPrefix(prefix, {exclude})``), ``@Module({controllers: [...]})`` classes, and
    ``RouterModule.register([{path, module, children}])`` entries. None when there is none."""
    boot_seen = any(m in source for m in _NEST_BOOT)
    has_module = b"@Module" in source
    if not boot_seen and not has_module:
        return None
    boot: tuple[list[tuple[str, str]] | None, NestExclude | None] | None = None
    modules: dict[str, list[str]] = {}
    register: list[tuple[str, str | None, list[tuple[str, str]] | None]] = []

    def entries(arr: Node, parent: str | None) -> None:
        for e in arr.named_children:
            if e.type != "object":
                continue
            pairs = {node_text(k, source).strip("'\""): v for kv in e.named_children
                     if kv.type == "pair" and (k := kv.child_by_field_name("key")) is not None
                     and (v := kv.child_by_field_name("value")) is not None}
            mod = pairs.get("module")
            name = node_text(mod, source) if mod is not None and mod.type == "identifier" else None
            if name is not None:
                path_v = pairs.get("path")  # optional: a path-less entry only groups children
                register.append((name, parent,
                                 _path_pieces(path_v, source) if path_v is not None else [("lit", "")]))
            kids = pairs.get("children")
            if kids is not None and kids.type == "array":
                entries(kids, name)

    def walk(n: Node) -> None:
        nonlocal boot
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            ftext = node_text(fn, source) if fn is not None else ""
            args = n.child_by_field_name("arguments")
            arg_nodes = list(args.named_children) if args is not None else []
            if ftext.endswith(".setGlobalPrefix") and arg_nodes:
                boot = (_path_pieces(arg_nodes[0], source),
                        _nest_exclude(arg_nodes[1] if len(arg_nodes) > 1 else None, source))
            elif ftext == "NestFactory.create" and boot is None:
                boot = ([], ())
            elif ftext == "RouterModule.register" and arg_nodes and arg_nodes[0].type == "array":
                entries(arg_nodes[0], None)
        if n.type == "decorator" and has_module:
            call = next((c for c in n.named_children if c.type == "call_expression"), None)
            fn = call.child_by_field_name("function") if call is not None else None
            cls = n.parent  # `export @Module(...) class X` → the decorator sits on the export
            if cls is not None and cls.type == "export_statement":
                cls = next((c for c in cls.named_children if c.type == "class_declaration"), None)
            if (fn is not None and node_text(fn, source) == "Module" and cls is not None
                    and cls.type == "class_declaration"):
                cname = cls.child_by_field_name("name")
                a = call.child_by_field_name("arguments") if call is not None else None
                obj = next((c for c in a.named_children if c.type == "object"), None) if a else None
                for kv in obj.named_children if obj is not None else []:
                    k = kv.child_by_field_name("key") if kv.type == "pair" else None
                    v = kv.child_by_field_name("value") if k is not None else None
                    if (cname is not None and k is not None and v is not None
                            and node_text(k, source) == "controllers" and v.type == "array"):
                        modules[node_text(cname, source)] = [
                            node_text(c, source) for c in v.named_children if c.type == "identifier"
                        ]
        for c in n.named_children:
            walk(c)

    walk(root)
    if boot is None and not modules and not register:
        return None
    return boot, modules, register


def _nest_prefixes(
    boots: list[tuple[str, tuple[list[tuple[str, str]] | None, NestExclude | None]]],
    modules: dict[str, list[str] | None],
    register: list[tuple[str, str | None, list[tuple[str, str]] | None]],
    const_values: dict[str, str | None],
) -> tuple[dict[str, tuple[str | None, NestExclude | None]], dict[str, str | None]]:
    """Resolve NestJS wiring once ``const_values`` is complete: per bootstrap directory, its
    global prefix (``""`` none, None unresolvable; two bootstraps in one directory that disagree
    → None); per controller of a ``RouterModule``-registered module, its composed path."""
    calls: dict[str, set[tuple[str | None, NestExclude | None]]] = {}
    for d, (pieces, exclude) in boots:
        calls.setdefault(d, set())
        if pieces != []:  # a setGlobalPrefix call ([] = NestFactory.create alone)
            calls[d].add((_resolve_pieces(pieces, const_values) if pieces else None, exclude))
    by_dir: dict[str, tuple[str | None, NestExclude | None]] = {
        d: ("", ()) if not found else next(iter(found)) if len(found) == 1 else (None, None)
        for d, found in calls.items()
    }
    own: dict[str, str | None] = {}
    parent: dict[str, str | None] = {}
    for mod, par, pieces in register:
        record_distinct(own, mod, _resolve_pieces(pieces, const_values) if pieces else None)
        record_distinct(parent, mod, par)

    def composed(mod: str, seen: frozenset[str]) -> str | None:
        p = own.get(mod)
        if p is None or mod in seen:
            return None
        par = parent.get(mod)
        if par is None:
            return _join_segments("", p)
        base = composed(par, seen | {mod})
        return _join_segments(base, p) if base is not None else None

    controllers: dict[str, str | None] = {}
    for mod in own:
        prefix = composed(mod, frozenset())
        for ctrl in modules.get(mod) or []:
            record_distinct(controllers, ctrl, prefix)
    return by_dir, controllers


def _collect(root: Node, source: bytes, rel: str, repo_root: object) -> tuple[str, _NestRaw] | None:
    """(this file's directory, its NestJS wiring)."""
    nest = _collect_nest(root, source)
    if nest is None:
        return None
    anchor = PurePosixPath(rel).parent.as_posix()
    return ("" if anchor == "." else anchor), nest


def _resolve(values: list[tuple[str, _NestRaw]], index: Any) -> NestPrefixes | None:
    boots: list[tuple[str, tuple[list[tuple[str, str]] | None, NestExclude | None]]] = []
    modules: dict[str, list[str] | None] = {}  # @Module class → controllers (name clash → None)
    register: list[tuple[str, str | None, list[tuple[str, str]] | None]] = []
    for anchor, (boot, mods, reg) in values:
        if boot is not None:
            boots.append((anchor, boot))
        for mname, ctrls in mods.items():
            record_distinct(modules, mname, ctrls)
        register.extend(reg)
    consts = getattr(index, "const_values", None) or {}
    global_prefixes, controller_prefixes = _nest_prefixes(boots, modules, register, consts)
    if not global_prefixes and not controller_prefixes:
        return None
    return NestPrefixes(global_prefixes, controller_prefixes)


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(*_NEST_BOOT, b"@Module"),
    collect=_collect, resolve=_resolve,
))
