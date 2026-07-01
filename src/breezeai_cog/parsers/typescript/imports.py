"""Import extraction + in-repo resolution for TypeScript/JavaScript.

Relative imports resolve directly; bare specifiers are external **unless** they match
a tsconfig ``compilerOptions.paths`` alias — those are resolved via the repo-level
``build_index`` result (a :class:`TsAliasIndex`) threaded in as ``resolution_index``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from ...utils import repo_relative
from ..treesitter import node_text

_SUFFIXES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_INDEXES = ("index.ts", "index.tsx", "index.js", "index.jsx")
_TSCONFIGS = ("tsconfig.json", "jsconfig.json")


@dataclass(frozen=True)
class TsAliasIndex:
    """tsconfig path-alias map (picklable — crosses the process boundary)."""

    base_dir: str  # absolute baseUrl directory
    paths: dict[str, list[str]]  # e.g. {"@app/*": ["src/app/*"]}


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
