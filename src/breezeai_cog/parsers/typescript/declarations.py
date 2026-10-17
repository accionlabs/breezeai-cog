"""What each TypeScript / JavaScript module exports, and where it is declared — a registered fact
on the TypeScript index, ``typescript.declarations``.

An entry point's handler is often declared in another file: a page component, a lazily loaded
route component, a handler function imported into a router. To lead the record to it (``handler``
+ ``handlerPath`` + ``handlerLine``), cog must know, for a module and an exported name, the
declaration's name, file and line::

    // src/components/pages/projects.tsx
    export default function ProjectList() { … }          // line 128
    // src/pages/index.ts
    export { default as ProjectList } from '../components/pages/projects';

    resolve_export(fact, "src/pages/index.ts", "ProjectList")
        → Declaration("ProjectList", "src/components/pages/projects.tsx", 128)

``_collect`` reads each file's exports: declared in the file (with the declaration's name and
line), re-exported from another file (``export { a as b } from './x'``, ``export *``), or an
imported name exported again. :func:`resolve_export` follows re-exports to the declaration. A
name exported twice differently, an export cycle, or a module outside the repo resolves to None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text

FACT = "typescript.declarations"
VUE_FACT = "typescript.vue-components"

_FUNCTION_NODES = ("function_declaration", "generator_function_declaration")
_CLASS_NODES = ("class_declaration", "abstract_class_declaration")


@dataclass(frozen=True)
class Declaration:
    #: The declared name; None for an anonymous default export (``export default () => …``).
    name: str | None
    file: str
    line: int


#: One export: ``("decl", name or "", line)`` — declared in this file — or
#: ``("from", file, name)`` — the export ``name`` of another repo file.
Export = tuple[str, str, int] | tuple[str, str, str]


@dataclass(frozen=True)
class FileExports:
    exports: dict[str, Export] = field(default_factory=dict)
    #: Files whose names this one re-exports with ``export * from``.
    stars: tuple[str, ...] = ()


def _resolve_relative(spec: str | None, rel: str, repo_root: Path | None) -> str | None:
    from .imports import _try_paths

    if spec is None or repo_root is None or not spec.startswith("."):
        return None
    return _try_paths((repo_root / rel).parent / spec, repo_root)


def _declared(decl: Node, source: bytes) -> list[tuple[str, int, Node]]:
    """(name, line, value or node) of each name one declaration statement declares:
    ``function F`` / ``class C`` / ``const X = …`` (each identifier declarator)."""
    if decl.type in _FUNCTION_NODES + _CLASS_NODES:
        nm = decl.child_by_field_name("name")
        return [(node_text(nm, source), decl.start_point[0] + 1, decl)] if nm is not None else []
    if decl.type in ("lexical_declaration", "variable_declaration"):
        out = []
        for d in decl.named_children:
            nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
            if nm is not None and nm.type == "identifier":
                value = d.child_by_field_name("value")
                out.append((node_text(nm, source), d.start_point[0] + 1,
                            value if value is not None else d))
        return out
    return []


def top_level_declarations(root: Node, source: bytes) -> dict[str, tuple[int, Node]]:
    """Top-level ``function`` / ``class`` / ``const X = …`` names → (line, the declaration's
    value or node), exported or not. A name declared twice is dropped."""
    found: dict[str, tuple[int, Node]] = {}
    twice: set[str] = set()
    for top in root.named_children:
        decl = top.child_by_field_name("declaration") or top if top.type == "export_statement" \
            else top
        for name, line, node in _declared(decl, source):
            if name in found:
                twice.add(name)
            found[name] = (line, node)
    for name in twice:
        del found[name]
    return found


def import_bindings(root: Node, source: bytes) -> dict[str, tuple[str, str]]:
    """Local name → (module specifier, the export it binds: ``default``, a name, or ``*``)."""
    from .imports import _module_of

    out: dict[str, tuple[str, str]] = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        module = _module_of(node, source)
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if module is None or clause is None:
            continue
        for c in clause.named_children:
            if c.type == "identifier":
                out[node_text(c, source)] = (module, "default")
            elif c.type == "namespace_import":
                ident = next((x for x in c.named_children if x.type == "identifier"), None)
                if ident is not None:
                    out[node_text(ident, source)] = (module, "*")
            elif c.type == "named_imports":
                for spec in c.named_children:
                    idents = [x for x in spec.named_children if x.type == "identifier"]
                    if spec.type == "import_specifier" and idents:
                        out[node_text(idents[-1], source)] = (module, node_text(idents[0], source))
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileExports | None:
    decls = top_level_declarations(root, source)
    imports = import_bindings(root, source)
    exports: dict[str, Export] = {}
    stars: list[str] = []

    def local(name: str) -> Export | None:
        """The export of a local name: its declaration here, or the import it re-exports."""
        if name in decls:
            return ("decl", name, decls[name][0])
        if name in imports:
            spec, imported = imports[name]
            target = _resolve_relative(spec, rel, repo_root)
            if target is not None and imported != "*":
                return ("from", target, imported)
        return None

    for node in root.named_children:
        if node.type != "export_statement":
            continue
        src = node.child_by_field_name("source")
        spec = node_text(src, source).strip("'\"`") if src is not None else None
        target = _resolve_relative(spec, rel, repo_root) if spec is not None else None
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if clause is not None:
            for s in clause.named_children:
                if s.type != "export_specifier":
                    continue
                name = s.child_by_field_name("name")
                alias = s.child_by_field_name("alias") or name
                if name is None or alias is None:
                    continue
                if spec is None:
                    found = local(node_text(name, source))
                    if found is not None:
                        exports[node_text(alias, source)] = found
                elif target is not None:
                    exports[node_text(alias, source)] = ("from", target, node_text(name, source))
            continue
        if spec is not None:  # `export * from './x'` (`* as ns` names a namespace, not a spread)
            if target is not None and not any(c.type == "namespace_export"
                                              for c in node.named_children):
                stars.append(target)
            continue
        decl = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        if any(c.type == "default" for c in node.children):
            inner = decl or value
            if inner is not None and inner.type == "identifier":
                found = local(node_text(inner, source))
                exports["default"] = found or ("decl", "", node.start_point[0] + 1)
            else:  # `export default function F() {}` / `class C {}` / an anonymous value
                nm = inner.child_by_field_name("name") if inner is not None else None
                exports["default"] = ("decl", node_text(nm, source) if nm is not None else "",
                                      (inner or node).start_point[0] + 1)
        elif decl is not None:
            for declared, line, _ in _declared(decl, source):
                exports[declared] = ("decl", declared, line)
    if not exports and not stars:
        return None
    return FileExports(exports, tuple(stars))


def _resolve(values: list[tuple[str, FileExports]], index: Any) -> dict[str, FileExports] | None:
    return dict(values) or None


def _collect_with_path(root: Node, source: bytes, rel: str,
                       repo_root: Path | None) -> tuple[str, FileExports] | None:
    found = _collect(root, source, rel, repo_root)
    return (rel, found) if found is not None else None


register_fact(FactSpec(
    name=FACT, language="typescript", gate=(b"export",),
    collect=_collect_with_path, resolve=_resolve,
))

# A Vue single-file component exports itself as `default`: its name is the `name` option
# (`defineOptions({ name: 'X' })`, or `name:` in `export default { … }`), else — Vue's own rule —
# the file name; its line is the `<script setup>` tag or the `export default`.
_VUE_NAME_RE = re.compile(rb"""(?:defineOptions\(\s*\{[^}]*?|export\s+default\s*(?:defineComponent\(\s*)?\{[^}]*?)\bname\s*:\s*['"]([\w$-]+)['"]""", re.S)
_VUE_DECL_RE = re.compile(rb"<script\b[^>]*\bsetup\b|export\s+default\b")


def _collect_vue(root: object, source: bytes, rel: str,
                 repo_root: Path | None) -> tuple[str, FileExports] | None:
    decl = _VUE_DECL_RE.search(source)
    if decl is None:
        return None
    m = _VUE_NAME_RE.search(source)
    name = m.group(1).decode() if m else Path(rel).stem
    line = source.count(b"\n", 0, decl.start()) + 1
    return rel, FileExports({"default": ("decl", name, line)})


register_fact(FactSpec(
    name=VUE_FACT, language="typescript", gate=(b"<script",), sources=(".vue",),
    collect=_collect_vue, resolve=_resolve,
))


def module_exports(index: Any | None) -> dict[str, FileExports]:
    return {**(index_fact(index, VUE_FACT) or {}), **(index_fact(index, FACT) or {})}


def resolve_export(index: Any | None, file: str, name: str,
                   _seen: frozenset[tuple[str, str]] = frozenset()) -> Declaration | None:
    """The declaration behind export ``name`` of ``file``, following re-exports; None when it
    can't be followed (not exported, outside the repo, a cycle)."""
    files = module_exports(index)
    if (file, name) in _seen:
        return None
    entry = files.get(file)
    if entry is None:
        return None
    seen = _seen | {(file, name)}
    exp = entry.exports.get(name)
    if exp is None:
        if name == "default":
            return None
        hits = {d for star in entry.stars if (d := resolve_export(index, star, name, seen))}
        return next(iter(hits)) if len(hits) == 1 else None
    if exp[0] == "decl":
        return Declaration(exp[1] or None, file, int(exp[2]))
    return resolve_export(index, str(exp[1]), str(exp[2]), seen)


def export_names(index: Any | None, file: str,
                 _seen: frozenset[str] = frozenset()) -> list[str]:
    """Every name ``file`` exports (``default`` excluded), ``export *`` included, in order."""
    entry = module_exports(index).get(file)
    if entry is None or file in _seen:
        return []
    names = [n for n in entry.exports if n != "default"]
    for star in entry.stars:
        names += [n for n in export_names(index, star, _seen | {file}) if n not in names]
    return names
