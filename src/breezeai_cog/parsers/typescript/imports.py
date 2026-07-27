"""Import extraction + in-repo resolution for TypeScript/JavaScript.

Relative imports resolve directly; bare specifiers are external **unless** they match
a tsconfig ``compilerOptions.paths`` alias — those are resolved via the repo-level
``build_index`` result (a :class:`TsAliasIndex`) threaded in as ``resolution_index``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...utils import repo_relative
from ..index_common import ClassHeritage, parallel_map, record_distinct
from ..treesitter import node_text, parse_source

_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_INDEXES = ("index.ts", "index.tsx", "index.js", "index.jsx")
_TSCONFIGS = ("tsconfig.json", "jsconfig.json")


@dataclass(frozen=True)
class TsAliasIndex:
    """Repo-wide TS resolution index (picklable — crosses the process boundary).

    Holds tsconfig path aliases plus a **string-constant value map**: repo-wide
    ``symbol → literal`` for top-level ``const``s, string ``enum`` members, and
    ``static readonly`` class fields (keyed ``NAME`` or ``Type.MEMBER``). A symbol
    declared with differing literals in >1 place maps to ``None`` (ambiguous → do not
    resolve through it — precision-first, mirroring the C# type index)."""

    base_dir: str  # absolute baseUrl directory
    paths: dict[str, list[str]]  # e.g. {"@app/*": ["src/app/*"]}
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


def build_alias_index(repo_root: Path) -> TsAliasIndex | None:
    for name in _TSCONFIGS:
        data = _load_jsonc(repo_root / name)
        if data is None:
            continue
        opts = data.get("compilerOptions") or {}
        paths = opts.get("paths")
        if not paths:
            continue
        base_dir = str((repo_root / opts.get("baseUrl", ".")).resolve())
        return TsAliasIndex(base_dir=base_dir, paths={k: list(v) for k, v in paths.items()})
    return None


def _string_literal(node: Node | None, source: bytes) -> str | None:
    """The value of a ``string`` node (empty string for ``''``), else None (not a literal)."""
    if node is None or node.type != "string":
        return None
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else ""


def _collect_const_values(root: Node, source: bytes, const_values: dict[str, str | None]) -> None:
    """Record ``symbol → literal`` from one file's top-level string constants:
    ``const NAME = 'x'``, ``enum E { M = 'x' }`` (→ ``E.M``), and ``static readonly M = 'x'``
    class fields (→ ``C.M``). Non-string values are skipped; a symbol seen with >1 distinct
    literal collapses to ``None`` (ambiguous, honest-null) via :func:`record_distinct`."""
    def add(sym: str, val: str | None) -> None:
        if val is not None:
            record_distinct(const_values, sym, val)

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
                        add(node_text(nm, source), _string_literal(d.child_by_field_name("value"), source))
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


def _ts_index_one(
    args: tuple[str, str]
) -> tuple[dict[str, str | None], dict[str, ClassHeritage], list[str], list[_MountRaw]] | None:
    """Parse one TS/JS file into its partials (string-constant map, class-heritage map, the
    classes it declares, and its lazy-route mounts) — pure, picklable worker for
    :func:`parallel_map`. Returns ``None`` on read/parse failure."""
    file_s, rel = args
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
        return const_values, _collect_heritage(root, src, rel), classes, mounts
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
    args = [(str(f), repo_relative(f, repo_root)) for f in files]
    for frag in parallel_map(args, _ts_index_one, jobs):
        if not frag:
            continue
        cv, heritage, classes, mounts = frag
        for sym, literal in cv.items():
            record_distinct(const_values, sym, literal)
        for cname, ch in heritage.items():  # same class name in >1 file → None (distinct types)
            record_distinct(class_heritage, cname, ch, same=lambda a, b: False)
        if mounts:
            file_mounts.append((frozenset(classes), mounts))

    # Now const_values is complete: resolve each mount's path and wire the graph. A module
    # symbol mounted at >1 distinct prefix (or an unresolved path) collapses to None — the
    # child then keeps its own bare path (honest-null), never a wrongly-attributed prefix.
    for classes, mounts in file_mounts:
        enclosing = next(iter(classes), None)  # the routing module this file declares (its mount owner)
        for target, pieces in mounts:
            record_distinct(own_prefix, target, _resolve_pieces(pieces, const_values))
            if enclosing is not None:
                record_distinct(parent_of, target, enclosing)
    route_mounts = _compose_route_mounts(own_prefix, parent_of)

    if alias is None and not const_values and not class_heritage and not route_mounts:
        return None
    return TsAliasIndex(
        base_dir=alias.base_dir if alias else str(repo_root),
        paths=alias.paths if alias else {},
        const_values=const_values,
        class_heritage=class_heritage,
        route_mounts=route_mounts,
    )


def _try_paths(target: Path, repo_root: Path) -> str | None:
    for suffix in _SUFFIXES:
        cand = target.with_name(target.name + suffix)
        if cand.is_file():
            return repo_relative(cand, repo_root)
    for index in _INDEXES:
        cand = target / index
        if cand.is_file():
            return repo_relative(cand, repo_root)
    return None


def _resolve_alias(module: str, index: TsAliasIndex, repo_root: Path) -> str | None:
    base = Path(index.base_dir)
    for pattern, targets in index.paths.items():
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # "@app/"
            if module.startswith(prefix):
                rest = module[len(prefix):]
                for tgt in targets:
                    sub = tgt[:-1] + rest if tgt.endswith("/*") else tgt
                    resolved = _try_paths(base / sub, repo_root)
                    if resolved:
                        return resolved
        elif module == pattern:
            for tgt in targets:
                resolved = _try_paths(base / tgt, repo_root)
                if resolved:
                    return resolved
    return None


def _resolve(module: str, file_path: str, repo_root: Path, index: TsAliasIndex | None) -> str | None:
    if module.startswith("."):
        return _try_paths((repo_root / file_path).parent / module, repo_root)
    if index is not None:
        return _resolve_alias(module, index, repo_root)
    return None


def _module_of(import_node: Node, source: bytes) -> str | None:
    src_node = import_node.child_by_field_name("source")
    if src_node is None:
        src_node = next((c for c in import_node.named_children if c.type == "string"), None)
    if src_node is None:
        return None
    frag = next((c for c in src_node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(src_node, source).strip("'\"")


def _export_names(node: Node, source: bytes) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        sub = stack.pop()
        if sub.type == "export_specifier":
            name = sub.child_by_field_name("name")
            if name is not None:
                names.append(node_text(name, source))
        elif sub.type in ("class_declaration", "function_declaration"):
            name = sub.child_by_field_name("name")
            if name is not None:
                names.append(node_text(name, source))
        stack.extend(sub.named_children)
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

    return list(internal), list(external), exports, bindings
