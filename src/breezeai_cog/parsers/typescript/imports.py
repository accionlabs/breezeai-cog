"""Import extraction + in-repo resolution for TypeScript/JavaScript.

Relative imports resolve directly; bare specifiers are external **unless** they match
a tsconfig ``compilerOptions.paths`` alias — those are resolved via the repo-level
``build_index`` result (a :class:`TsAliasIndex`) threaded in as ``resolution_index``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from tree_sitter import Node

from ...utils import repo_relative
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

    Holds tsconfig path aliases plus a **string-constant value map**: repo-wide
    ``symbol → literal`` for top-level ``const``s, string ``enum`` members, and
    ``static readonly`` class fields (keyed ``NAME`` or ``Type.MEMBER``). A symbol
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
    #: Angular lazy-route mount linkage: a routing-module symbol → its **fully composed**
    #: effective mount prefix (parent chain prepended), so a child module parsed in its own
    #: file can start at that prefix instead of "". Chain-composed and ambiguity-collapsed in
    #: :func:`build_ts_index`; a symbol mounted at >1 prefix (or with an unresolved chain) → not
    #: present (honest-null — the child then falls back to its own bare path).
    route_mounts: dict[str, str] = field(default_factory=dict)
    #: Express mount linkage: a mounted router-factory's defining FILE (repo-relative) → the base
    #: path it is served under (``app.use('/users', usersRouter())`` → ``users-router.ts`` maps
    #: to ``/users``). A factory file mounted at >1 distinct base collapses to None and is
    #: dropped (honest-null — its routes then keep their own bare path); test/fixture mounts are
    #: not collected. Consumed by ``detect_express`` to prefix a factory's router-local routes
    #: with their real base — the Express analogue of ``route_mounts`` for Angular lazy routes.
    express_mounts: dict[str, str] = field(default_factory=dict)


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


def _iter_tsconfigs(repo_root: Path) -> Iterator[Path]:
    """Every ``tsconfig*.json`` / ``jsconfig.json`` in the tree, node_modules & build output
    pruned. tsconfigs are ``.json``, so they go to the config parser — the alias index must
    find them by its own bounded walk, not the TypeScript file list."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fn in filenames:
            if fn == "jsconfig.json" or (fn.startswith("tsconfig") and fn.endswith(".json")):
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
    """Collect tsconfig path aliases from **every** tsconfig in the tree (not just the root),
    each scope carrying absolute targets so an app's ``@/*`` resolves against its own package.
    Scopes are ordered deepest-first for nearest-config resolution."""
    scopes: list[tuple[str, dict[str, list[str]]]] = []
    for cfg in _iter_tsconfigs(repo_root):
        paths: dict[str, list[str]] = {}
        _collect_tsconfig_paths(cfg, set(), paths)  # fresh cycle-guard per config
        if paths:
            scopes.append((str(cfg.parent.resolve()), paths))
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
            val = resolve_const_value(inner, source, consts) if inner is not None else None
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
    a ``const X = {…} as const`` object (→ ``X.a.b``, folding templates + const refs). Non-string
    values are skipped; a symbol seen with >1 distinct literal collapses to ``None`` (honest-null)."""
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
            for d in node.named_children:
                if d.type == "variable_declarator":
                    nm = d.child_by_field_name("name")
                    if nm is not None and nm.type == "identifier":
                        value = _unwrap_const_expr(d.child_by_field_name("value"))
                        add(node_text(nm, source), _string_literal(value, source))
                        if value is not None and value.type == "object":
                            obj_consts[node_text(nm, source)] = value
        elif node.type == "enum_declaration":
            ename = next((c for c in node.named_children if c.type in ("identifier", "type_identifier")), None)
            body = next((c for c in node.named_children if c.type == "enum_body"), None)
            if ename is not None and body is not None:
                for m in body.named_children:
                    if m.type == "enum_assignment":
                        mn = next((c for c in m.named_children if c.type == "property_identifier"), None)
                        mv = next((c for c in m.named_children if c.type == "string"), None)
                        if mn is not None:
                            add(f"{node_text(ename, source)}.{node_text(mn, source)}", _string_literal(mv, source))
        elif node.type == "class_declaration":
            cname = next((c for c in node.named_children if c.type in ("type_identifier", "identifier")), None)
            body = next((c for c in node.named_children if c.type == "class_body"), None)
            if cname is not None and body is not None:
                for f in body.named_children:
                    if f.type == "public_field_definition":
                        fn = next((c for c in f.named_children if c.type == "property_identifier"), None)
                        fv = next((c for c in f.named_children if c.type == "string"), None)
                        if fn is not None:
                            add(f"{node_text(cname, source)}.{node_text(fn, source)}", _string_literal(fv, source))

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


# --- Angular lazy-route mount extraction (picklable worker side) ---------------------------
#
# A path expression is returned as UNRESOLVED pieces — ``("lit", text)`` for a string literal
# and ``("sym", "RouteNames.X")`` for a const reference — because ``RouteNames.*`` is defined
# cross-file, so the pieces are resolved against the repo-wide ``const_values`` in the reduce
# (:func:`_resolve_pieces`), not here.
_MountRaw = tuple[str, list[tuple[str, str]]]  # (target module symbol, path pieces)

# `.then(m => m.OrganisationsRoutingModule)` — the mounted module's exported class symbol.
_MOUNT_TARGET_RE = re.compile(r"=>\s*[\w$]+\.([A-Za-z_$][\w$]*)")


def _path_pieces(node: Node | None, source: bytes) -> list[tuple[str, str]] | None:
    """A route ``path`` value → ordered resolvable pieces, or None if any piece is dynamic
    (a call, a template substitution that isn't a plain member/identifier). Mirrors the
    angular detector's resolution but yields pieces to resolve later against ``const_values``."""
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


def _collect_route_mounts(root: Node, source: bytes) -> tuple[list[str], list[_MountRaw]]:
    """(top-level class names declared here, lazy mounts declared here). A mount is a
    ``loadChildren`` pair inside a route object; its target is the ``.then(m => m.X)`` symbol
    and its own path is the sibling ``path:`` value (as unresolved pieces)."""
    classes: list[str] = []
    mounts: list[_MountRaw] = []

    def walk(node: Node) -> None:
        if node.type in _CLASS_NODES:
            nm = node.child_by_field_name("name")
            if nm is not None:
                classes.append(node_text(nm, source))
        if node.type == "object":  # a route config object literal
            pairs = {}
            for c in node.named_children:
                if c.type == "pair":
                    k = c.child_by_field_name("key")
                    if k is not None:
                        pairs[node_text(k, source).strip("'\"")] = c.child_by_field_name("value")
            if "loadChildren" in pairs and "path" in pairs:
                lc = pairs["loadChildren"]
                m = _MOUNT_TARGET_RE.search(node_text(lc, source)) if lc is not None else None
                pieces = _path_pieces(pairs["path"], source)
                if m is not None and pieces is not None:
                    mounts.append((m.group(1), pieces))
        for c in node.named_children:
            walk(c)

    walk(root)
    return classes, mounts


# --- Express mount extraction (picklable worker side) --------------------------------------
#
# An Express router factory (``const r = () => { const route = Router(); route.get(...); return
# route }``) lives in its own file and is mounted elsewhere with ``app.use('/base', factory())``.
# The base path and the factory's routes sit in different files, so — like the Angular lazy
# mounts above — the linkage resolves through the repo-wide index: this collector records, at
# each mount site, ``defining-file → base`` so the factory file can later prefix its own routes.

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
    return out


def _mount_receiver_ok(obj_text: str) -> bool:
    """Whether a ``X.use(...)`` receiver looks like an Express app/router — mirrors the runtime
    detector's ``_is_router_obj`` heuristic (duplicated to keep the framework parser out of the
    index module's imports)."""
    tail = obj_text.lower().rsplit(".", 1)[-1].strip()
    return tail in {"app", "router", "server", "api", "route"} or tail.endswith(("router", "app"))


def _factory_call_name(arg: Node, source: bytes) -> str | None:
    """If ``arg`` is a bare ``factory()`` call, its callee identifier name; else None. Member
    calls (``Auth.check()``) and non-calls are skipped."""
    if arg.type != "call_expression":
        return None
    fn = arg.child_by_field_name("function")
    return node_text(fn, source) if fn is not None and fn.type == "identifier" else None


def _collect_express_mounts(
    root: Node, source: bytes, rel: str, repo_root: Path
) -> list[tuple[str, str]]:
    """(mounted-factory file, base path) for each ``app.use('/base', factory())`` in this file.
    The factory identifier is resolved to its defining file via this file's relative imports. A
    mount whose base is not a plain string, or whose factory is not a single relative-imported
    bare call, is skipped — honest-null, never a guessed link. Test/fixture files are not a mount
    source (their throwaway bases must not define a factory's real prefix)."""
    if b".use(" not in source or _is_fixture_path(rel):
        return []
    import_map = _relative_import_map(root, source)
    if not import_map:
        return []
    mount_dir = (repo_root / rel).parent
    out: list[tuple[str, str]] = []

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
                obj = fn.child_by_field_name("object")
                if (prop is not None and node_text(prop, source) == "use"
                        and obj is not None and _mount_receiver_ok(node_text(obj, source))):
                    args = node.child_by_field_name("arguments")
                    arg_nodes = list(args.named_children) if args is not None else []
                    base = _string_literal(arg_nodes[0], source) if arg_nodes else None
                    if base is not None and base.startswith("/"):
                        specs = [
                            import_map[name]
                            for a in arg_nodes[1:]
                            if (name := _factory_call_name(a, source)) is not None
                            and name in import_map
                        ]
                        if len(specs) == 1:  # exactly one imported factory arg → unambiguous
                            resolved = _try_paths(mount_dir / specs[0], repo_root)
                            if resolved is not None:
                                out.append((resolved, base))
        for c in node.named_children:
            walk(c)

    walk(root)
    return out


_CLASS_NODES_SET = frozenset(_CLASS_NODES)


def _ngmodule_import_names(dec: Node, source: bytes) -> list[str] | None:
    """Given a ``decorator`` node, return the list of identifier names from its
    ``@NgModule({imports: [...]})`` array, or None if it is not an NgModule decorator."""
    call = next((c for c in dec.named_children if c.type == "call_expression"), None)
    if call is None:
        return None
    fn = call.child_by_field_name("function")
    if fn is None or node_text(fn, source) != "NgModule":
        return None
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    config = next((c for c in args.named_children if c.type == "object"), None)
    if config is None:
        return None
    for pair in config.named_children:
        if pair.type != "pair":
            continue
        k = pair.child_by_field_name("key")
        v = pair.child_by_field_name("value")
        if k is None or node_text(k, source).strip("'\"") != "imports":
            continue
        if v is None or v.type != "array":
            continue
        return [node_text(c, source) for c in v.named_children if c.type == "identifier"]
    return None


def _collect_ngmodule_imports(root: Node, source: bytes) -> dict[str, list[str]]:
    """Return a map of class name → list of identifier names from the class's
    ``@NgModule({imports: [...]})`` decorator. Only top-level class declarations
    are considered (the Angular module pattern). Returns empty dict if no NgModule found."""
    if b"NgModule" not in source:
        return {}
    result: dict[str, list[str]] = {}
    pending: list[str] | None = None  # imports from the most recent @NgModule decorator

    for child in root.named_children:
        if child.type == "decorator":
            pending = _ngmodule_import_names(child, source)
            continue
        if child.type == "comment":
            continue  # comments don't clear pending decorators (mirrors TypeScriptParser.extract)

        # inline decorator inside export_statement (export @NgModule class Foo {})
        if child.type == "export_statement":
            inner_dec = next((c for c in child.named_children if c.type == "decorator"), None)
            if inner_dec is not None:
                imports = _ngmodule_import_names(inner_dec, source)
                if imports is not None:
                    pending = imports
            cls_node = next((c for c in child.named_children if c.type in _CLASS_NODES_SET), None)
        else:
            cls_node = child if child.type in _CLASS_NODES_SET else None

        if cls_node is not None and pending is not None:
            nm = cls_node.child_by_field_name("name")
            if nm is not None:
                result[node_text(nm, source)] = pending
        pending = None

    return result


def _ts_index_one(
    args: tuple[str, str, str]
) -> tuple[dict[str, str | None], dict[str, ClassHeritage], list[str], list[_MountRaw], dict[str, list[str]], list[tuple[str, str]]] | None:
    """Parse one TS/JS file into its partials (string-constant map, class-heritage map, the
    classes it declares, its lazy-route mounts, NgModule imports, and Express factory mounts) —
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
        classes, mounts = _collect_route_mounts(root, src)
        ngmodule_imports = _collect_ngmodule_imports(root, src)
        express_mounts = _collect_express_mounts(root, src, rel, Path(repo_root_s))
        return (const_values, _collect_heritage(root, src, rel), classes, mounts,
                ngmodule_imports, express_mounts)
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
    """Join two route segments with a single slash, ignoring empties (mirrors the angular
    detector's ``_join`` so composed prefixes match within-file composition)."""
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _compose_route_mounts(
    raw: dict[str, str | None], parent_of: dict[str, str | None]
) -> dict[str, str]:
    """Chain-compose each mount's own prefix with its ancestors' prefixes to a full effective
    path. ``raw[sym]`` is the module's OWN mount prefix (None if ambiguous/unresolved);
    ``parent_of[sym]`` is the module symbol whose mount encloses it (None if none/ambiguous).
    A module reached through an unresolved link → dropped (honest-null). Iterates to a fixpoint,
    bounded by chain length; a cycle (shouldn't happen in routing) just stops resolving."""
    composed: dict[str, str] = {}
    for _ in range(len(raw) + 1):  # bounded: converges in <= chain-depth passes
        progressed = False
        for sym, own in raw.items():
            if sym in composed or own is None:
                continue
            parent = parent_of.get(sym)
            # A root boundary: no parent, an ambiguous parent (None), or a parent that is not
            # itself a mounted module (e.g. the top AppRoutingModule) → own prefix IS the path.
            if parent is None or parent not in raw:
                composed[sym] = own
                progressed = True
            elif parent in composed:  # ancestor resolved → prepend its full chain
                composed[sym] = _join_segments(composed[parent], own)
                progressed = True
        if not progressed:
            break
    return composed


def build_ts_index(repo_root: Path, files: Sequence[Path], jobs: int = 1) -> TsAliasIndex | None:
    """Repo-level pre-pass: tsconfig aliases, a string-constant value map, class heritage
    (for inherited-call resolution), and Angular lazy-route mount linkage — parses each file
    once, across ``jobs`` processes. Returns None only when there is nothing to resolve with."""
    alias = build_alias_index(repo_root)  # repo-level (tsconfig) — computed once in main
    const_values: dict[str, str | None] = {}
    class_heritage: dict[str, ClassHeritage | None] = {}
    # Mount graph, collected raw then resolved after const_values is complete (paths reference
    # cross-file consts). ``own_prefix``: module symbol → its own mount prefix (ambiguity via
    # record_distinct). ``parent_of``: module symbol → the module whose file declared its mount.
    own_prefix: dict[str, str | None] = {}
    parent_of: dict[str, str | None] = {}
    file_mounts: list[tuple[frozenset[str], list[_MountRaw]]] = []
    all_ngmodule_imports: dict[str, list[str]] = {}
    # Express: mounted-factory file → its base path. A factory file mounted at >1 distinct base
    # collapses to None (honest-null) via record_distinct; the None entries are dropped below.
    express_mount_of: dict[str, str | None] = {}
    args = [(str(f), repo_relative(f, repo_root), str(repo_root)) for f in files]
    for frag in parallel_map(args, _ts_index_one, jobs):
        if not frag:
            continue
        cv, heritage, classes, mounts, ngmodule_imports, file_express_mounts = frag
        for sym, literal in cv.items():
            record_distinct(const_values, sym, literal)
        for cname, ch in heritage.items():  # same class name in >1 file → None (distinct types)
            record_distinct(class_heritage, cname, ch, same=lambda a, b: False)
        if mounts:
            file_mounts.append((frozenset(classes), mounts))
        all_ngmodule_imports.update(ngmodule_imports)
        for factory_file, base in file_express_mounts:
            record_distinct(express_mount_of, factory_file, base)

    # Now const_values is complete: resolve each mount's path and wire the graph. A module
    # symbol mounted at >1 distinct prefix (or an unresolved path) collapses to None — the
    # child then keeps its own bare path (honest-null), never a wrongly-attributed prefix.
    for classes, mounts in file_mounts:
        enclosing = next(iter(classes), None)  # the routing module this file declares (its mount owner)
        for target, pieces in mounts:
            record_distinct(own_prefix, target, _resolve_pieces(pieces, const_values))
            if enclosing is not None:
                record_distinct(parent_of, target, enclosing)
    # Pre-composition NgModule propagation: if BrandModule is a loadChildren target and its
    # @NgModule imports BrandRoutingModule, inject BrandRoutingModule into own_prefix/parent_of
    # with the same own-path and parent as BrandModule BEFORE _compose_route_mounts runs.
    # This makes BrandRoutingModule a proper node in the composition graph, so that
    # ProductsModule (whose parent_of entry is BrandRoutingModule) chains correctly all the
    # way up to /company/:brandId/products instead of stopping at /products.
    for class_name, ngmod_imports in all_ngmodule_imports.items():
        own = own_prefix.get(class_name)
        if own is None:
            continue  # class not a loadChildren target, or path is ambiguous
        parent = parent_of.get(class_name)
        for imported in ngmod_imports:
            if imported not in own_prefix:
                record_distinct(own_prefix, imported, own)
                if parent is not None:
                    record_distinct(parent_of, imported, parent)

    route_mounts = _compose_route_mounts(own_prefix, parent_of)

    # Post-composition NgModule propagation: for modules that are now in route_mounts after
    # composition (e.g. ProductsModule → /company/:brandId/products), also add their NgModule-
    # imported routing modules (e.g. ProductsRoutingModule) so _mount_prefix() finds them.
    for class_name, prefix in list(route_mounts.items()):
        for imported_name in all_ngmodule_imports.get(class_name, []):
            if imported_name not in route_mounts:
                route_mounts[imported_name] = prefix

    # Drop ambiguous (None) Express mounts — a factory mounted at conflicting bases keeps its
    # own bare paths rather than being wrongly attributed to one of them (honest-null).
    express_mounts = {k: v for k, v in express_mount_of.items() if v is not None}

    if (alias is None and not const_values and not class_heritage and not route_mounts
            and not express_mounts):
        return None
    return TsAliasIndex(
        base_dir=alias.base_dir if alias else str(repo_root),
        paths=alias.paths if alias else {},
        alias_scopes=alias.alias_scopes if alias else (),
        const_values=const_values,
        class_heritage=class_heritage,
        route_mounts=route_mounts,
        express_mounts=express_mounts,
    )


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

    # Dynamic imports (`import('x')`) — lazy pages / code-split components. Resolved into the
    # same file-level edge set as static imports (no binding: a dynamic import binds a Promise,
    # not a clean symbol, so calls[].path can't attribute through it — honest-null).
    if _DYNAMIC_IMPORT_GUARD in source:
        for module in _dynamic_import_specifiers(root, source):
            resolved = _resolve(module, file_path, repo_root, index)
            (internal if resolved else external).setdefault(resolved or module, None)

    return list(internal), list(external), exports, bindings
