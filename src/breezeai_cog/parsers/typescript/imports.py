"""Import extraction + in-repo resolution for TypeScript/JavaScript.

Relative imports resolve directly; bare specifiers are external **unless** they match
a tsconfig ``compilerOptions.paths`` alias or a Vite ``resolve.alias`` entry (see
:mod:`.vite_aliases`) — those are resolved via the repo-level ``build_index`` result (a
:class:`TsAliasIndex`) threaded in as ``resolution_index``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Sequence

from tree_sitter import Node

from ...utils import repo_relative
from ..additive import (
    collect_additive, collect_fact_sources, resolve_additive, split_fact_sources,
)
from ..index_common import ClassHeritage, parallel_map, record_distinct
from ..treesitter import node_text, parse_source

# `.vue` is appended LAST so an extensionless import next to both `Foo.ts` and `Foo.vue`
# still resolves to the JS/TS file (matching bundler resolution order). Only `.vue` is added
# here — the Vue parser emits a real `File` node for it, so the edge lands on a graph identity;
# extensions with no parser (e.g. `.svelte`) are deliberately omitted to avoid dangling edges.
_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".vue")
_INDEXES = ("index.ts", "index.tsx", "index.js", "index.jsx", "index.vue")


@dataclass(frozen=True)
class TsAliasIndex:
    """Repo-wide TS resolution index (picklable — crosses the process boundary).

    Holds tsconfig path aliases plus a **constant value map**: repo-wide
    ``symbol → literal`` for top-level ``const``s, ``enum`` members with an initializer, and
    ``static readonly`` class fields (keyed ``NAME`` or ``Type.MEMBER``) whose value is a string
    or a number (as JavaScript writes it in a string: ``5_000`` → ``5000``). A symbol
    declared with differing literals in >1 place maps to ``None`` (ambiguous → do not
    resolve through it — precision-first, mirroring the C# type index)."""

    base_dir: str  # absolute baseUrl directory (legacy single-config resolution)
    paths: dict[str, list[str]]  # e.g. {"@app/*": ["src/app/*"]} — relative to base_dir (legacy)
    #: Per-tsconfig alias scopes: ``(config_dir_abs, {pattern: [absolute targets]})`` sorted
    #: deepest-first. A monorepo declares aliases in *per-package* tsconfigs (an app's ``@/*``
    #: means *its own* ``src``), so resolution picks the **nearest** enclosing config — never a
    #: sibling package's same-named alias. When set, this supersedes ``paths``/``base_dir``.
    alias_scopes: tuple[tuple[str, dict[str, list[str]]], ...] = ()
    const_values: dict[str, str | None] = field(default_factory=dict)
    #: simple class name → heritage (base + method→file), for resolving inherited `this.M()`
    #: calls to the declaring base file. A name declared in >1 file → ``None`` (ambiguous).
    class_heritage: dict[str, ClassHeritage | None] = field(default_factory=dict)
    #: Facts registered with ``register_fact`` (fact name → fact), resolved once before
    #: parsing. Read with :func:`~..additive.index_fact`; see ``parsers/additive.py``.
    facts: dict[str, Any] = field(default_factory=dict)


def _load_jsonc(path: Path) -> dict | None:
    """Tolerant JSON-with-comments loader for tsconfig files."""
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # block comments
    text = re.sub(r"(^|\s)//.*$", "", text, flags=re.M)  # line comments (whitespace-prefixed)
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # trailing commas
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


#: Directories never worth walking for tsconfigs (dependencies + build output).
_PRUNE_DIRS = frozenset({"node_modules", "dist", "build", "out", ".next", "coverage", ".git"})


#: Extensions of a Vite config (``vite.config.ts``, ``vite.config.mjs``, …).
_VITE_CONFIG_EXTS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")


def _is_vite_config(fn: str) -> bool:
    return fn.startswith("vite.config.") and fn.endswith(_VITE_CONFIG_EXTS)


def _iter_alias_configs(repo_root: Path) -> Iterator[Path]:
    """Every ``tsconfig*.json`` / ``jsconfig.json`` / ``vite.config.*`` in the tree, node_modules
    & build output pruned. tsconfigs are ``.json``, so they go to the config parser — the alias
    index must find them by its own bounded walk, not the TypeScript file list."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in sorted(filenames):
            if (fn == "jsconfig.json" or (fn.startswith("tsconfig") and fn.endswith(".json"))
                    or _is_vite_config(fn)):
                yield Path(dirpath) / fn


def _abs_targets(base_url_dir: Path, targets: list[str]) -> list[str]:
    """Resolve tsconfig ``paths`` targets to absolute strings, preserving a trailing ``/*``."""
    out: list[str] = []
    for tgt in targets:
        star = tgt.endswith("/*")
        core = tgt[:-2] if star else tgt
        abs_core = str((base_url_dir / core).resolve())
        out.append(abs_core + "/*" if star else abs_core)
    return out


def _collect_tsconfig_paths(cfg: Path, seen: set[Path], out: dict[str, list[str]]) -> None:
    """Merge one tsconfig's ``compilerOptions.paths`` (absolute targets) into ``out``, then
    follow a **local** ``extends`` (relative/absolute path only — a package-name base lives in
    node_modules and is left unresolved, honest-null). Cycle-guarded via ``seen``."""
    if cfg in seen:
        return
    seen.add(cfg)
    data = _load_jsonc(cfg)
    if data is None:
        return
    opts = data.get("compilerOptions") or {}
    base_url_dir = (cfg.parent / opts.get("baseUrl", ".")).resolve()
    for pattern, targets in (opts.get("paths") or {}).items():
        if isinstance(targets, list):
            lst = out.setdefault(pattern, [])
            for abs_tgt in _abs_targets(base_url_dir, [str(t) for t in targets]):
                if abs_tgt not in lst:
                    lst.append(abs_tgt)
    ext = data.get("extends")
    for e in (ext if isinstance(ext, list) else [ext]):
        if isinstance(e, str) and (e.startswith(".") or e.startswith("/")):
            base_cfg = (cfg.parent / e).resolve()
            if base_cfg.suffix != ".json":
                base_cfg = base_cfg / "tsconfig.json"
            _collect_tsconfig_paths(base_cfg, seen, out)


def build_alias_index(repo_root: Path) -> TsAliasIndex | None:
    """Collect path aliases from **every** tsconfig and Vite config in the tree (not just the
    root), each scope carrying absolute targets so an app's ``@/*`` resolves against its own
    package. Scopes are ordered deepest-first for nearest-config resolution; where a tsconfig
    and a Vite config share a folder, the tsconfig's scope is tried first."""
    from .vite_aliases import vite_alias_scope  # it imports this module

    scopes: list[tuple[str, dict[str, list[str]]]] = []
    vite_scopes: list[tuple[str, dict[str, list[str]]]] = []
    for cfg in _iter_alias_configs(repo_root):
        if _is_vite_config(cfg.name):
            vite_scope = vite_alias_scope(cfg, repo_root)
            if vite_scope is not None:
                vite_scopes.append(vite_scope)
            continue
        paths: dict[str, list[str]] = {}
        _collect_tsconfig_paths(cfg, set(), paths)  # fresh cycle-guard per config
        if paths:
            scopes.append((str(cfg.parent.resolve()), paths))
    scopes += vite_scopes
    if not scopes:
        return None
    scopes.sort(key=lambda s: len(Path(s[0]).parts), reverse=True)
    return TsAliasIndex(base_dir=str(repo_root), paths={}, alias_scopes=tuple(scopes))


def _string_literal(node: Node | None, source: bytes) -> str | None:
    """The value of a ``string`` node (empty string for ``''``), else None (not a literal)."""
    if node is None or node.type != "string":
        return None
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else ""


_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]*[1-9]$")


def number_literal(node: Node | None, source: bytes) -> str | None:
    """A ``number`` node as JavaScript renders it in a string (`` `${n}` ``): an integer in any
    base (``5_000`` / ``0x10`` → ``5000`` / ``16``) or a plain decimal (``1.5``); anything
    else (an exponent, ``1.0``, a legacy octal ``010``) → None rather than a guess."""
    if node is None or node.type != "number":
        return None
    text = node_text(node, source).replace("_", "")
    try:
        return str(int(text, 0))
    except ValueError:
        return text if _DECIMAL_RE.match(text) else None


def _literal(node: Node | None, source: bytes, *, constant: bool) -> str | None:
    """A string literal's value, or a number literal's (see :func:`number_literal`) when the
    declaration is ``constant`` (``const`` / ``readonly``): a ``let`` number is often a counter."""
    if node is not None and node.type == "number":
        return number_literal(node, source) if constant else None
    return _string_literal(node, source)


# ── reusable constant-value resolution ─────────────────────────────────────────
# These resolve a value that is built from a *constant* (a symbol, a member path into a
# ``{…} as const`` object, or a template of those) to its literal string — the general primitive
# behind route ``endpoint`` / api-call URL / event-address folding. They read the repo-wide
# ``const_values`` map (flat + dotted keys) and are honest-null: an unresolved value → None.


def member_path(node: Node, source: bytes) -> str | None:
    """A member-expression / identifier → its dotted symbol path (``paths.discover.root``), or
    None if it is not a plain identifier/member chain (a computed ``a[k]`` breaks it)."""
    parts: list[str] = []
    n: Node | None = node
    while n is not None and n.type == "member_expression":
        prop = n.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier":
            return None
        parts.append(node_text(prop, source))
        n = n.child_by_field_name("object")
    if n is not None and n.type == "identifier":
        parts.append(node_text(n, source))
        return ".".join(reversed(parts))
    return None


def _unwrap_const_expr(node: Node | None) -> Node | None:
    """Strip ``as const`` / ``satisfies`` / parentheses to the underlying value node."""
    while node is not None and node.type in (
        "as_expression", "satisfies_expression", "parenthesized_expression",
    ):
        node = node.named_children[0] if node.named_children else None
    return node


def _pair_key_name(key: Node | None, source: bytes) -> str | None:
    if key is None:
        return None
    if key.type == "string":
        return _string_literal(key, source)
    if key.type in ("property_identifier", "identifier"):
        return node_text(key, source)
    return None  # computed key [x] → skip


def resolve_const_value(node: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    """A value node → its literal string via ``consts``: a string literal, a template whose every
    ``${…}`` resolves, or a member/identifier reference. None if not fully resolvable (honest-null).
    The call site every detector uses to fold a const-built address into its literal."""
    if node.type == "string":
        return _string_literal(node, source)
    if node.type == "template_string":
        return _resolve_template(node, source, consts)
    if node.type in ("identifier", "member_expression"):
        key = member_path(node, source)
        return consts.get(key) if key is not None else None
    return None


def _resolve_template(node: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    out: list[str] = []
    for c in node.children:
        if c.type == "string_fragment":
            out.append(node_text(c, source))
        elif c.type == "template_substitution":
            inner = c.named_children[0] if c.named_children else None
            val = None if inner is None else (
                number_literal(inner, source) if inner.type == "number"
                else resolve_const_value(inner, source, consts))
            if val is None:
                return None  # any unresolved substitution → whole template unresolved
            out.append(val)
    return "".join(out)


def flatten_const_object(
    base: str, obj: Node, source: bytes,
    str_consts: dict[str, str | None], obj_consts: dict[str, Node],
    out: dict[str, str], seen: frozenset[str] = frozenset(), depth: int = 0,
) -> None:
    """Flatten a ``{…} as const`` object literal into ``base.a.b → value`` for every leaf that
    resolves to a string (literal / template / const-ref), following an object-valued reference
    (``tabs: sharedTabs``) into that object. Non-literal / unresolved leaves are skipped
    (honest-null); recursion is depth- and cycle-bounded."""
    if depth > 6:
        return

    def add_ref(path: str, ref: str) -> None:
        """A property whose value is an identifier: inline-flatten a referenced object const,
        else resolve a referenced scalar const."""
        if ref in obj_consts and ref not in seen:
            flatten_const_object(path, obj_consts[ref], source, str_consts, obj_consts,
                                 out, seen | {ref}, depth + 1)
        else:
            r = str_consts.get(ref)
            if r is not None:
                out[path] = r

    for prop in obj.named_children:
        if prop.type == "shorthand_property_identifier":  # `{ tabs }` == `tabs: tabs`
            name = node_text(prop, source)
            add_ref(f"{base}.{name}", name)
            continue
        if prop.type != "pair":
            continue
        kname = _pair_key_name(prop.child_by_field_name("key"), source)
        val = _unwrap_const_expr(prop.child_by_field_name("value"))
        if kname is None or val is None:
            continue
        path = f"{base}.{kname}"
        if val.type == "object":
            flatten_const_object(path, val, source, str_consts, obj_consts, out, seen, depth + 1)
        elif val.type == "identifier":
            add_ref(path, node_text(val, source))
        else:
            resolved = resolve_const_value(val, source, str_consts)
            if resolved is not None:
                out[path] = resolved


def _collect_const_values(root: Node, source: bytes, const_values: dict[str, str | None]) -> None:
    """Record ``symbol → literal`` from one file's top-level constants: ``const NAME = 'x'``,
    ``enum E { M = 'x' }`` (→ ``E.M``), ``static readonly M = 'x'`` (→ ``C.M``), and the leaves of
    a ``const X = {…} as const`` object (→ ``X.a.b``, folding templates + const refs). Values
    other than a string or a number are skipped, and a number only counts when declared constant
    (``const`` / ``readonly`` / an enum member); a symbol seen with >1 distinct literal collapses
    to ``None`` (honest-null)."""
    def add(sym: str, val: str | None) -> None:
        if val is not None:
            record_distinct(const_values, sym, val)

    obj_consts: dict[str, Node] = {}  # const name → object-literal node (for §pass 2 flattening)
    for child in root.named_children:
        node: Node | None = child
        if child.type == "export_statement":
            node = next((c for c in child.named_children if c.type in (
                "lexical_declaration", "variable_declaration",
                "enum_declaration", "class_declaration")), None)
        if node is None:
            continue
        if node.type in ("lexical_declaration", "variable_declaration"):
            constant = bool(node.children) and node.children[0].type == "const"
            for d in node.named_children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    if nm is not None and nm.type == "identifier":
                        value = _unwrap_const_expr(d.child_by_field_name("value"))
                        add(node_text(nm, source), _literal(value, source, constant=constant))
                        if value is not None and value.type == "object":
                            obj_consts[node_text(nm, source)] = value
        elif node.type == "enum_declaration":
            ename = next((c for c in node.named_children if c.type in ("identifier", "type_identifier")), None)
            body = next((c for c in node.named_children if c.type == "enum_body"), None)
            if ename is not None and body is not None:
                for m in body.named_children:
                    if m.type == "enum_assignment":
                        mn = next((c for c in m.named_children if c.type == "property_identifier"), None)
                        mv = next((c for c in m.named_children if c.type in ("string", "number")), None)
                        if mn is not None:
                            add(f"{node_text(ename, source)}.{node_text(mn, source)}", _literal(mv, source, constant=True))
        elif node.type == "class_declaration":
            cname = next((c for c in node.named_children if c.type in ("type_identifier", "identifier")), None)
            body = next((c for c in node.named_children if c.type == "class_body"), None)
            if cname is not None and body is not None:
                for f in body.named_children:
                    if f.type == "public_field_definition":
                        fn = next((c for c in f.named_children if c.type == "property_identifier"), None)
                        fv = next((c for c in f.named_children if c.type in ("string", "number")), None)
                        readonly = any(c.type == "readonly" for c in f.children)
                        if fn is not None:
                            add(f"{node_text(cname, source)}.{node_text(fn, source)}",
                                _literal(fv, source, constant=readonly))

    # Pass 2: flatten object consts now that flat consts are collected (their leaves may reference
    # a sibling flat const via a `${…}` template, e.g. `root: `/${segment}``).
    for name, obj in obj_consts.items():
        flat: dict[str, str] = {}
        flatten_const_object(name, obj, source, const_values, obj_consts, flat)
        for k, v in flat.items():
            add(k, v)


_CLASS_NODES = ("class_declaration", "class", "abstract_class_declaration")


def _collect_heritage(root: Node, source: bytes, rel: str) -> dict[str, ClassHeritage]:
    """Each class declared in this file → its heritage: base class name + the methods it
    declares mapped to this file. (A TS class lives in one file, so every method → ``rel``.)"""
    from .classes import _heritage  # lazy — avoid an import cycle with classes.py

    out: dict[str, ClassHeritage] = {}

    def walk(node: Node) -> None:
        for child in node.named_children:
            if child.type in _CLASS_NODES:
                nm = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if nm is not None:
                    extends, _ = _heritage(child, source)
                    methods: dict[str, str | None] = {}
                    for m in (body.named_children if body is not None else []):
                        if m.type == "method_definition":
                            mn = m.child_by_field_name("name")
                            if mn is not None:
                                methods[node_text(mn, source)] = rel
                    out[node_text(nm, source)] = ClassHeritage(extends=extends, decorators=[], methods=methods)
            walk(child)

    walk(root)
    return out


def _path_pieces(node: Node | None, source: bytes) -> list[tuple[str, str]] | None:
    """A route ``path`` value → ordered resolvable pieces, or None if any piece is dynamic
    (a call, a template substitution that isn't a plain member/identifier). The pieces resolve
    later against ``const_values``, once every file's constants are known."""
    if node is None:
        return None
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return [("lit", node_text(frag, source) if frag is not None else "")]
    if node.type in ("member_expression", "identifier"):
        return [("sym", node_text(node, source))]
    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is None or node_text(op, source) != "+":
            return None
        left = _path_pieces(node.child_by_field_name("left"), source)
        right = _path_pieces(node.child_by_field_name("right"), source)
        return (left + right) if (left is not None and right is not None) else None
    if node.type == "template_string":
        pieces: list[tuple[str, str]] = []
        for child in node.named_children:
            if child.type == "string_fragment":
                pieces.append(("lit", node_text(child, source)))
            elif child.type == "template_substitution":
                inner = next((c for c in child.named_children), None)
                sub = _path_pieces(inner, source)
                if sub is None:
                    return None
                pieces.extend(sub)
            else:
                return None
        return pieces
    return None


# Filename infixes / dirs whose mounts are test scaffolding, not the wired app (mirrors
# base._GLOBAL_FIXTURE_MARKERS + TypeScriptParser._TS_FIXTURE_MARKERS; kept in sync by hand to
# avoid an import cycle with the parser module). A test that mounts a factory at a throwaway base
# must not define — or make ambiguous — that factory's real mount prefix.
_FIXTURE_MOUNT_MARKERS = (".test.", ".spec.", ".stories.", ".cy.", ".e2e.", ".mock.")
_FIXTURE_MOUNT_DIRS = frozenset({"mock", "mocks", "__mocks__", "fixtures", "__fixtures__"})


def _is_fixture_path(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(m in parts[-1] for m in _FIXTURE_MOUNT_MARKERS):
        return True
    return any(seg in _FIXTURE_MOUNT_DIRS for seg in parts[:-1])


def require_specifier(node: Node | None, source: bytes) -> str | None:
    """``require('./x')`` → ``'./x'`` when the specifier is relative; else None."""
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if fn is None or node_text(fn, source) != "require" or args is None:
        return None
    first = args.named_children[0] if args.named_children else None
    spec = _string_literal(first, source)
    return spec if spec is not None and spec.startswith(".") else None


def relative_requires(root: Node, source: bytes) -> dict[str, tuple[str, str]]:
    """CommonJS counterpart of ES ``import`` bindings — top-level
    ``const x = require('./a')`` → ``x: ('./a', 'default')`` (``module.exports`` is the default)
    and ``const { r, s: t } = require('./a')`` → ``r: ('./a', 'r')``, ``t: ('./a', 's')``."""
    out: dict[str, tuple[str, str]] = {}
    if b"require(" not in source:
        return out
    for node in root.named_children:
        if node.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            module = require_specifier(d.child_by_field_name("value"), source)
            name = d.child_by_field_name("name")
            if module is None or name is None:
                continue
            if name.type == "identifier":
                out[node_text(name, source)] = (module, "default")
            elif name.type == "object_pattern":
                for p in name.named_children:
                    if p.type == "shorthand_property_identifier_pattern":
                        out[node_text(p, source)] = (module, node_text(p, source))
                    elif p.type == "pair_pattern":
                        key, value = p.child_by_field_name("key"), p.child_by_field_name("value")
                        if key is not None and value is not None and value.type == "identifier":
                            out[node_text(value, source)] = (module, node_text(key, source))
    return out


def _relative_import_map(root: Node, source: bytes) -> dict[str, str]:
    """local imported name → its **relative** module specifier (only ``.``-prefixed, since a
    factory mount is resolved file-to-file in the reduce). Bare/aliased specifiers are omitted."""
    out: dict[str, str] = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        module = _module_of(node, source)
        if module is None or not module.startswith("."):
            continue
        for nm in _imported_names(node, source):
            out[nm] = module
    for nm, (module, _) in relative_requires(root, source).items():
        out.setdefault(nm, module)
    return out


def _ts_index_one(
    args: tuple[str, str, str]
) -> tuple[dict[str, str | None], dict[str, ClassHeritage], dict[str, Any]] | None:
    """Parse one TS/JS file into its partials (string-constant map, class-heritage map, the
    and what
    registered facts collect) —
    pure, picklable worker for :func:`parallel_map`. Returns ``None`` on read/parse failure."""
    file_s, rel, repo_root_s = args
    if not Path(file_s).is_file():
        return None
    try:
        src = Path(file_s).read_bytes()
    except OSError:
        return None
    grammar = "tsx" if file_s.endswith((".tsx", ".jsx")) else "typescript"
    try:
        root = parse_source(grammar, src, 0).root_node
        const_values: dict[str, str | None] = {}
        _collect_const_values(root, src, const_values)
        return (const_values, _collect_heritage(root, src, rel),
                collect_additive("typescript", root, src, rel, Path(repo_root_s)))
    except Exception as exc:  # parse OR a pathologically deep AST walk (RecursionError) — skip this file
        from ...logging import get_logger
        get_logger("breezeai_cog.index").warning(
            "index.file.skipped", path=file_s, language="typescript",
            error_type=type(exc).__name__, error=str(exc),
        )
        return None


def _resolve_pieces(pieces: list[tuple[str, str]], const_values: dict[str, str | None]) -> str | None:
    """Join a path's resolvable pieces to a literal, or None if any const piece is unresolved
    (missing from the index, or ambiguous → None there). Honest-null: never a guessed segment."""
    out: list[str] = []
    for kind, text in pieces:
        if kind == "lit":
            out.append(text)
        else:  # "sym" — a const/enum/static-field reference resolved cross-file
            val = const_values.get(text)
            if val is None:
                return None
            out.append(val)
    return "".join(out)


def _join_segments(base: str, sub: str) -> str:
    """Join two route segments with a single slash, ignoring empties (``/api`` + ``users/`` →
    ``/api/users``)."""
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def build_ts_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> TsAliasIndex | None:
    """Repo-level pre-pass: tsconfig aliases, a string-constant value map, class heritage
    (for inherited-call resolution) and the registered facts — parses each file once, across
    ``jobs`` processes. Returns None only when there is nothing to resolve with."""
    alias = build_alias_index(repo_root)  # repo-level (tsconfig) — computed once in main
    files, fact_files = split_fact_sources("typescript", list(files))
    const_values: dict[str, str | None] = {}
    class_heritage: dict[str, ClassHeritage | None] = {}
    # registered fact name → per-file values: fact-source files (another parser's), then our own
    collected: dict[str, list[Any]] = collect_fact_sources("typescript", fact_files, repo_root, jobs)
    args = [(str(f), repo_relative(f, repo_root), str(repo_root)) for f in files]
    for frag in parallel_map(args, _ts_index_one, jobs):
        if not frag:
            continue
        (cv, heritage, facts) = frag
        for name, value in facts.items():
            collected.setdefault(name, []).append(value)
        for sym, literal in cv.items():
            record_distinct(const_values, sym, literal)
        for cname, ch in heritage.items():  # same class name in >1 file → None (distinct types)
            record_distinct(class_heritage, cname, ch, same=lambda a, b: False)

    index = TsAliasIndex(
        base_dir=alias.base_dir if alias else str(repo_root),
        paths=alias.paths if alias else {},
        alias_scopes=alias.alias_scopes if alias else (),
        const_values=const_values,
        class_heritage=class_heritage,
    )
    # Registered facts may need the whole index, so they resolve after it is built.
    facts = resolve_additive("typescript", collected, index)
    if (alias is None and not const_values and not class_heritage
            and not any(v is not None for v in facts.values())):
        return None
    return replace(index, facts=facts)


#: ESM / NodeNext specifiers name the *emitted* file (`./x.js`) while the source on disk is
#: TypeScript (`./x.ts` / `.tsx`). tsc never rewrites the extension in the specifier, so a
#: literal `.js` probe misses. Map each JS-family suffix to the TS siblings to try instead.
_EXT_REWRITE = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}


def _try_paths(target: Path, repo_root: Path) -> str | None:
    # An explicit-extension target (Vue imports are conventionally `./Avatar.vue`) resolves
    # as-is; the suffix loop below only ever APPENDS an extension, so without this an explicit
    # `.vue`/`.ts` specifier would be probed as `Avatar.vue.vue` and missed.
    if target.is_file():
        return repo_relative(target, repo_root)
    # `./x.js` → `./x.ts`/`.tsx`. Gated on is_file, so it only ever resolves to a real File
    # node (honest-null); the literal `.js` is tried first above, so a repo that genuinely
    # ships `x.js` beside `x.ts` still binds to the real `.js`.
    for ts_suffix in _EXT_REWRITE.get(target.suffix, ()):
        cand = target.with_suffix(ts_suffix)
        if cand.is_file():
            return repo_relative(cand, repo_root)
    for suffix in _SUFFIXES:
        cand = target.with_name(target.name + suffix)
        if cand.is_file():
            return repo_relative(cand, repo_root)
    for index in _INDEXES:
        cand = target / index
        if cand.is_file():
            return repo_relative(cand, repo_root)
    return None


def _match_alias(
    module: str, paths: dict[str, list[str]], repo_root: Path, base: Path | None
) -> str | None:
    """Resolve ``module`` against one alias map. ``base`` joins **relative** targets (legacy
    single-config); ``None`` means targets are already absolute (per-scope index)."""
    def loc(tgt: str) -> Path:
        return base / tgt if base is not None else Path(tgt)

    for pattern, targets in paths.items():
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # "@app/"
            if module.startswith(prefix):
                rest = module[len(prefix):]
                for tgt in targets:
                    sub = tgt[:-1] + rest if tgt.endswith("/*") else tgt
                    resolved = _try_paths(loc(sub), repo_root)
                    if resolved:
                        return resolved
        elif module == pattern:
            for tgt in targets:
                resolved = _try_paths(loc(tgt), repo_root)
                if resolved:
                    return resolved
    return None


def _resolve_alias(module: str, index: TsAliasIndex, repo_root: Path, file_path: str) -> str | None:
    if index.alias_scopes:
        # Nearest-config wins: try each enclosing tsconfig scope deepest-first, so an app's
        # `@/*` resolves against its own `src`, never a sibling package's same-named alias.
        file_dir = (repo_root / file_path).parent.resolve()
        for cfg_dir, paths in index.alias_scopes:
            cd = Path(cfg_dir)
            if cd == file_dir or cd in file_dir.parents:
                resolved = _match_alias(module, paths, repo_root, None)
                if resolved:
                    return resolved
        return None
    return _match_alias(module, index.paths, repo_root, Path(index.base_dir))


def _resolve(module: str, file_path: str, repo_root: Path, index: TsAliasIndex | None) -> str | None:
    if module.startswith("."):
        return _try_paths((repo_root / file_path).parent / module, repo_root)
    if index is not None:
        return _resolve_alias(module, index, repo_root, file_path)
    return None


def _module_of(import_node: Node, source: bytes) -> str | None:
    src_node = import_node.child_by_field_name("source")
    if src_node is None:
        src_node = next((c for c in import_node.named_children if c.type == "string"), None)
    if src_node is None:
        return None
    frag = next((c for c in src_node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(src_node, source).strip("'\"")


def imported_locals(root: Node, source: bytes, module: str, wanted: frozenset[str]) -> set[str]:
    """Local names bound to any of ``wanted`` imported from ``module`` (handles ``as`` alias).
    ``import { useState as useS } from 'react'`` with ``wanted={"useState"}`` → ``{"useS"}``.
    Used to detect framework-primitive calls (Vue reactivity, React hooks) by the *local*
    binding rather than the bare name, so aliasing and same-named locals don't confuse it."""
    locals_set: set[str] = set()
    for node in root.named_children:
        if node.type != "import_statement" or _module_of(node, source) != module:
            continue
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for c in clause.named_children:
            if c.type != "named_imports":
                continue
            for spec in c.named_children:
                if spec.type != "import_specifier":
                    continue
                idents = [x for x in spec.named_children if x.type == "identifier"]
                # `{ useState }` -> [useState]; `{ useState as useS }` -> [useState, useS].
                # First is the imported name, last is the local binding.
                if idents and node_text(idents[0], source) in wanted:
                    locals_set.add(node_text(idents[-1], source))
    return locals_set


def _export_names(node: Node, source: bytes) -> list[str]:
    """Exported names of one ``export_statement``. Scans the statement's TOP-LEVEL structure
    only — never descending into a declaration's value — so a nested local (``export const X =
    defineComponent({ setup(){ const n = 1 } })``) is not mistaken for an export."""
    names: list[str] = []
    if node.type != "export_statement":
        return names
    if any(c.type == "default" for c in node.children):
        # `export default …` provides the module's default binding (a named
        # `export default function foo(){}` also yields `foo` via the declaration branch below).
        names.append("default")
    for child in node.named_children:
        if child.type == "export_clause":  # `export { a, b as c }` / `export { … } from '…'`
            for spec in child.named_children:
                if spec.type == "export_specifier":
                    # exported name is the alias when present, else the bare name
                    name = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                    if name is not None:
                        names.append(node_text(name, source))
        elif child.type in ("class_declaration", "function_declaration"):
            name = child.child_by_field_name("name")
            if name is not None:
                names.append(node_text(name, source))
        elif child.type in ("lexical_declaration", "variable_declaration"):
            # `export const X = …` (incl. `export const a = 1, b = 2`). Only the top-level
            # declarators — a destructuring pattern (non-identifier name) is skipped (honest-null).
            for d in child.named_children:
                if d.type == "variable_declarator":
                    name = d.child_by_field_name("name")
                    if name is not None and name.type == "identifier":
                        names.append(node_text(name, source))
    return names


def _imported_names(node: Node, source: bytes) -> list[str]:
    """Local names a TS import binds: default, ``* as ns``, and ``{a, b as c}`` (→ c)."""
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is None:
        return []
    names: list[str] = []
    for c in clause.named_children:
        if c.type == "identifier":  # default import
            names.append(node_text(c, source))
        elif c.type == "namespace_import":  # * as ns
            ident = next((x for x in c.named_children if x.type == "identifier"), None)
            if ident is not None:
                names.append(node_text(ident, source))
        elif c.type == "named_imports":
            for spec in c.named_children:
                if spec.type == "import_specifier":
                    idents = [x for x in spec.named_children if x.type == "identifier"]
                    if idents:  # last identifier = alias if present, else the name
                        names.append(node_text(idents[-1], source))
    return names


# A dynamic import is `import('x')` — a call_expression whose `function` child is the `import`
# node. It can sit anywhere (a lazy route `component: () => import(...)`, `defineAsyncComponent`,
# an `await import()`), so unlike a static import it isn't a top-level statement. The byte guard
# short-circuits the tree walk for the ~all files that use no dynamic import.
_DYNAMIC_IMPORT_GUARD = b"import("


def _dynamic_import_specifiers(root: Node, source: bytes) -> list[str]:
    """Every dynamic ``import('x')`` specifier in the tree. A non-literal specifier
    (a template/concat computed path) yields no string node and is skipped — honest-null,
    never a guessed path."""
    out: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "import":
                args = n.child_by_field_name("arguments")
                strings = args.named_children if args is not None else []
                s = next((c for c in strings if c.type == "string"), None)
                spec = _string_literal(s, source)
                if spec:
                    out.append(spec)
        stack.extend(n.named_children)
    return out


# CommonJS ``require('x')`` — read like a static import. A ``require`` with a string literal can
# sit anywhere (top level, inside a function, a side-effect ``require('./polyfill')``), so the
# edges come from a tree walk; bindings only from top-level declarations, like ESM imports.
_REQUIRE_GUARD = b"require("


def _require_module(node: Node | None, source: bytes) -> str | None:
    """``require('x')`` / ``require('x').member`` → ``'x'`` (any literal specifier); else None.
    A dynamic ``require(name)`` has no literal and yields nothing (honest-null)."""
    if node is not None and node.type == "member_expression":
        node = node.child_by_field_name("object")
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if fn is None or fn.type != "identifier" or node_text(fn, source) != "require" or args is None:
        return None
    first = args.named_children[0] if args.named_children else None
    return _string_literal(first, source) or None


def _require_specifiers(root: Node, source: bytes) -> list[str]:
    """Every literal ``require('x')`` specifier in the tree, in source order."""
    out: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            spec = _require_module(n, source)
            if spec:
                out.append(spec)
        stack.extend(reversed(n.named_children))
    return out


def _require_bindings(root: Node, source: bytes) -> dict[str, str]:
    """Local name → module for top-level CommonJS bindings: ``const x = require('m')``,
    ``const x = require('m').y``, ``const { a, b: c } = require('m')`` (``var`` / ``let`` too)."""
    out: dict[str, str] = {}
    for node in root.named_children:
        if node.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            module = _require_module(d.child_by_field_name("value"), source)
            name = d.child_by_field_name("name")
            if module is None or name is None:
                continue
            if name.type == "identifier":
                out[node_text(name, source)] = module
            elif name.type == "object_pattern":
                for p in name.named_children:
                    if p.type == "shorthand_property_identifier_pattern":
                        out[node_text(p, source)] = module
                    elif p.type == "pair_pattern":
                        value = p.child_by_field_name("value")
                        if value is not None and value.type == "identifier":
                            out[node_text(value, source)] = module
    return out


def extract_imports(
    root: Node, source: bytes, file_path: str, repo_root: str | Path, index: TsAliasIndex | None = None
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    repo_root = Path(repo_root)
    internal: dict[str, None] = {}
    external: dict[str, None] = {}
    exports: list[str] = []
    bindings: dict[str, str] = {}  # imported name → in-repo file (calls[].path)

    for node in root.named_children:
        if node.type == "import_statement":
            module = _module_of(node, source)
            if module is None:
                continue
            resolved = _resolve(module, file_path, repo_root, index)
            (internal if resolved else external).setdefault(resolved or module, None)
            if resolved:
                for nm in _imported_names(node, source):
                    bindings[nm] = resolved
        elif node.type == "export_statement":
            exports.extend(_export_names(node, source))
            # A re-export (`export … from './X'`) — a barrel-file edge. Resolve the source so
            # the barrel becomes a real graph waypoint (consumer → barrel → definition) instead
            # of a dead end. Gate on the `source` field, not a string search, so a plain
            # `export const s = 'hello'` is never mistaken for a re-export. All forms (named,
            # `default as`, `export *`, `* as ns`) are covered — only the source is resolved.
            if node.child_by_field_name("source") is not None:
                module = _module_of(node, source)
                if module:
                    resolved = _resolve(module, file_path, repo_root, index)
                    (internal if resolved else external).setdefault(resolved or module, None)

    # CommonJS `require('x')` — the same edges as an ESM import; top-level `const x = require(…)`
    # also binds `x` to the resolved file, so calls[].path resolves through it.
    if _REQUIRE_GUARD in source:
        resolved_of: dict[str, str | None] = {}
        for module in _require_specifiers(root, source):
            if module not in resolved_of:
                resolved_of[module] = _resolve(module, file_path, repo_root, index)
            resolved = resolved_of[module]
            (internal if resolved else external).setdefault(resolved or module, None)
        for nm, module in _require_bindings(root, source).items():
            resolved = resolved_of.get(module)
            if resolved and nm not in bindings:
                bindings[nm] = resolved

    # Dynamic imports (`import('x')`) — lazy pages / code-split components. Resolved into the
    # same file-level edge set as static imports (no binding: a dynamic import binds a Promise,
    # not a clean symbol, so calls[].path can't attribute through it — honest-null).
    if _DYNAMIC_IMPORT_GUARD in source:
        for module in _dynamic_import_specifiers(root, source):
            resolved = _resolve(module, file_path, repo_root, index)
            (internal if resolved else external).setdefault(resolved or module, None)

    return list(internal), list(external), exports, bindings
