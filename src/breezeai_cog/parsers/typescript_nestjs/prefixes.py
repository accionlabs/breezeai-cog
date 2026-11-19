"""NestJS address prefixes as a registered fact on the TypeScript index, ``nest.prefixes``.

Two kinds of wiring put a prefix in front of a controller's routes, usually from another file::

    // main.ts — the app's global prefix (every controller below this directory)
    app.setGlobalPrefix('api', { exclude: ['health'] });
    // app.module.ts — a RouterModule path (the controllers of the registered module)
    RouterModule.register([{ path: 'admin', module: AdminModule }]);
    @Module({ controllers: [UsersController] }) export class AdminModule {}

* ``global_prefixes``: the directory of a bootstrap file (``NestFactory.create`` /
  ``app.setGlobalPrefix``) → (prefix, exclude). prefix ``""`` = the app sets none,
  ``RUNTIME_PREFIX`` = set from a value that does not resolve (config / env), ``None`` = two
  bootstraps in the directory disagree; exclude = ``((path, METHOD | None), …)``, ``None`` when it
  cannot be read. A controller belongs to the deepest bootstrap directory above it.
* ``controller_prefixes``: controller class name → its module's composed ``RouterModule`` path
  (``None`` when ambiguous or unresolvable). Only controllers of registered modules.
* ``versioning``: the directory of a bootstrap file that calls ``app.enableVersioning(…)`` → its
  :class:`Versioning` (type, URI prefix, default version). A directory with no such call is
  absent: cog did not see versioning turned on there.

Prefix pieces may name constants of other files, so they resolve against the index's constant
map once it is complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..index_common import record_distinct
from ..treesitter import node_text
from ..typescript.imports import _join_segments, _path_pieces, _resolve_pieces, _string_literal

FACT = "nest.prefixes"
#: A global prefix that is set from a runtime value (``setGlobalPrefix(config.x)``): the
#: routes' address is unknown, but the part after the prefix is known (``endpointSuffix``).
RUNTIME_PREFIX = "\0runtime"

#: A NestJS ``setGlobalPrefix`` exclude list: ``((path, METHOD | None), …)``.
NestExclude = tuple[tuple[str, str | None], ...]

_NEST_BOOT = (b"NestFactory", b"setGlobalPrefix")

#: ``VersioningType`` members, by the name written in the source.
_VERSIONING_TYPES = frozenset({"URI", "HEADER", "MEDIA_TYPE", "CUSTOM"})

#: A version list as declared: each entry a version string, or None for ``VERSION_NEUTRAL``.
Versions = tuple[str | None, ...]


@dataclass(frozen=True)
class Versioning:
    """One app's ``enableVersioning`` options. ``kind`` is the ``VersioningType`` name, or None
    when the options cannot be read (a variable, a computed value) — then nothing about the
    app's versioning is known. ``prefix`` is the URI segment prefix (``v`` unless set; ``""`` for
    ``prefix: false``). ``default`` is ``defaultVersion``, None when the app sets none."""

    kind: str | None
    prefix: str = "v"
    default: Versions | None = None


UNKNOWN_VERSIONING = Versioning(None)

#: One file's NestJS facts: (bootstrap: (prefix pieces | ``[]`` none | None dynamic, exclude) or
#: None, @Module class → its controllers, RouterModule entries (module, parent module, pieces),
#: the file's ``enableVersioning`` options or None).
_NestRaw = tuple[
    tuple[list[tuple[str, str]] | None, NestExclude | None] | None,
    dict[str, list[str]],
    list[tuple[str, str | None, list[tuple[str, str]] | None]],
    Versioning | None,
]


def version_values(text: str) -> Versions | None:
    """A version expression as written → its versions: ``'2'`` → ``("2",)``, ``['1', '2']`` →
    ``("1", "2")``, ``VERSION_NEUTRAL`` → ``(None,)``. None when any part is not a literal (a
    constant, a call): its value is not in the source."""
    text = text.strip()
    parts = ([p for p in (x.strip() for x in text[1:-1].split(",")) if p]
             if text.startswith("[") and text.endswith("]") else [text])
    out: list[str | None] = []
    for part in parts:
        if part.rsplit(".", 1)[-1] == "VERSION_NEUTRAL":
            out.append(None)
        elif len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"`" and "${" not in part:
            out.append(part[1:-1])
        else:
            return None
    return tuple(out) if out else None


def _versioning(opts: Node | None, source: bytes) -> Versioning:
    """``app.enableVersioning({ type: VersioningType.URI, prefix: 'v', defaultVersion: '1' })``
    → :class:`Versioning`. No argument is NestJS's default, URI versioning. Any option that is
    not written literally makes the whole setting unknown."""
    if opts is None:
        return Versioning("URI")
    if opts.type != "object":
        return UNKNOWN_VERSIONING
    kind: str | None = "URI"
    prefix = "v"
    default: Versions | None = None
    for pair in opts.named_children:
        key = pair.child_by_field_name("key") if pair.type == "pair" else None
        value = pair.child_by_field_name("value") if key is not None else None
        if key is None or value is None:
            return UNKNOWN_VERSIONING
        name = node_text(key, source).strip("'\"")
        text = node_text(value, source)
        if name == "type":
            kind = text.rsplit(".", 1)[-1] if value.type == "member_expression" else None
            if kind not in _VERSIONING_TYPES:
                return UNKNOWN_VERSIONING
        elif name == "prefix":
            if text == "false":
                prefix = ""
            elif value.type == "string" and (lit := _string_literal(value, source)) is not None:
                prefix = lit
            else:
                return UNKNOWN_VERSIONING
        elif name == "defaultVersion":
            default = version_values(text)
            if default is None:
                return UNKNOWN_VERSIONING
    return Versioning(kind, prefix, default)


@dataclass(frozen=True)
class NestPrefixes:
    global_prefixes: dict[str, tuple[str | None, NestExclude | None]]
    controller_prefixes: dict[str, str | None]
    versioning: dict[str, Versioning] = field(default_factory=dict)


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
    versioning: Versioning | None = None
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
        nonlocal boot, versioning
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            ftext = node_text(fn, source) if fn is not None else ""
            args = n.child_by_field_name("arguments")
            arg_nodes = list(args.named_children) if args is not None else []
            if ftext.endswith(".setGlobalPrefix") and arg_nodes:
                boot = (_path_pieces(arg_nodes[0], source),
                        _nest_exclude(arg_nodes[1] if len(arg_nodes) > 1 else None, source))
            elif ftext.endswith(".enableVersioning"):
                found = _versioning(arg_nodes[0] if arg_nodes else None, source)
                versioning = found if versioning in (None, found) else UNKNOWN_VERSIONING
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
    if boot is None and not modules and not register and versioning is None:
        return None
    return boot, modules, register, versioning


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
            resolved = _resolve_pieces(pieces, const_values) if pieces else None
            calls[d].add((RUNTIME_PREFIX if resolved is None else resolved, exclude))
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
    versioning: dict[str, Versioning] = {}
    for anchor, (boot, mods, reg, ver) in values:
        if boot is not None:
            boots.append((anchor, boot))
        if ver is not None:  # two bootstraps in one directory that disagree → unknown
            versioning[anchor] = ver if versioning.get(anchor, ver) == ver else UNKNOWN_VERSIONING
        for mname, ctrls in mods.items():
            record_distinct(modules, mname, ctrls)
        register.extend(reg)
    consts = getattr(index, "const_values", None) or {}
    global_prefixes, controller_prefixes = _nest_prefixes(boots, modules, register, consts)
    if not global_prefixes and not controller_prefixes and not versioning:
        return None
    return NestPrefixes(global_prefixes, controller_prefixes, versioning)


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(*_NEST_BOOT, b"@Module"),
    collect=_collect, resolve=_resolve,
))
