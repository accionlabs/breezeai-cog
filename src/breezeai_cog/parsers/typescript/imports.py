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
from ..index_common import record_distinct
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


def build_ts_index(repo_root: Path, files: Sequence[Path]) -> TsAliasIndex | None:
    """Repo-level pre-pass: tsconfig aliases + a string-constant value map (parses each TS
    file once). Returns None only when there are neither aliases nor constants."""
    alias = build_alias_index(repo_root)
    const_values: dict[str, str | None] = {}
    for f in files:
        try:
            src = Path(f).read_bytes()
        except OSError:
            continue
        grammar = "tsx" if str(f).endswith((".tsx", ".jsx")) else "typescript"
        try:
            root = parse_source(grammar, src, 0).root_node
        except Exception:
            continue
        _collect_const_values(root, src, const_values)
    if alias is None and not const_values:
        return None
    return TsAliasIndex(
        base_dir=alias.base_dir if alias else str(repo_root),
        paths=alias.paths if alias else {},
        const_values=const_values,
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
