"""A TypeScript / JavaScript file's exports and **relative** re-exports, for following a name to
the file that declares it::

    // handler.ts
    export { sender } from './sqs/sender';            // sender → sqs/sender.ts, sender
    export { onOrder as recv } from './sqs/receiver'; // recv → sqs/receiver.ts, onOrder
    export * from './jobs';                           // any name ./jobs exports
    import { ping } from './ping'; export { ping };   // ping → ping.ts, ping
    export { default as sender } from './sqs/sender'; // sqs/sender.ts: export default sender;

Only relative specifiers are followed (``./`` / ``../``), resolved the way the TypeScript
index resolves a relative import (:func:`.imports._try_paths`): a path alias or a package is
left alone. Collected per file during the TypeScript index pass (top-level statements only);
:func:`declaring` follows a name through at most a few hops. A name two ``export *`` targets both
provide is ambiguous (TypeScript rejects it too) and resolves to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tree_sitter import Node

from ..treesitter import node_text

_MAX_HOPS = 4


@dataclass(frozen=True)
class ModuleExports:
    #: The file, without its extension (``src/handler``).
    stem: str
    #: Names it declares and exports itself.
    declared: frozenset[str]
    #: Re-exported name → (target stem, the name there).
    renames: tuple[tuple[str, str, str], ...]
    #: ``export * from`` targets.
    stars: tuple[str, ...]
    #: The local name exported as ``default`` (``export default sender;`` /
    #: ``export default function sender``), when it has one.
    default: str | None = None


def _stem(rel: str) -> str:
    return str(PurePosixPath(rel).with_suffix(""))


def _specifier(node: Node, source: bytes) -> str | None:
    src = node.child_by_field_name("source")
    frag = next((c for c in src.named_children if c.type == "string_fragment"), None) \
        if src is not None else None
    return node_text(frag, source) if frag is not None else None


def collect_exports(root: Node, source: bytes, rel: str, repo_root: Path | None) -> ModuleExports | None:
    """This file's exports, or None when it neither declares nor re-exports anything."""
    from .imports import _try_paths

    def target(spec: str | None) -> str | None:
        if repo_root is None or spec is None or not spec.startswith("."):
            return None  # only relative specifiers: no alias or package resolution
        resolved = _try_paths((repo_root / rel).parent / spec, repo_root)
        return _stem(resolved) if resolved is not None else None

    declared: set[str] = set()
    renames: list[tuple[str, str, str]] = []
    stars: list[str] = []
    # local → (specifier, imported name); resolved only if re-exported (resolving stats files)
    imported: dict[str, tuple[str, str]] = {}
    default: str | None = None
    for node in root.named_children:
        if node.type == "import_statement":
            spec_text = _specifier(node, source)
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            named = next((c for c in clause.named_children if c.type == "named_imports"), None) \
                if clause is not None and spec_text is not None and spec_text.startswith(".") \
                else None
            for spec in named.named_children if named is not None else []:
                name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                if spec.type == "import_specifier" and name is not None and spec_text is not None:
                    imported[node_text(alias or name, source)] = (spec_text, node_text(name, source))
            continue
        if node.type != "export_statement":
            continue
        value = node.child_by_field_name("value")
        if value is not None and value.type == "identifier":  # export default sender;
            default = node_text(value, source)
            continue
        decl = node.child_by_field_name("declaration")
        if decl is not None and any(c.type == "default" for c in node.children):
            name = decl.child_by_field_name("name")  # export default function sender() {}
            default = node_text(name, source) if name is not None else None
            continue
        if decl is not None:
            if decl.type in ("function_declaration", "class_declaration",
                             "generator_function_declaration"):
                name = decl.child_by_field_name("name")
                if name is not None:
                    declared.add(node_text(name, source))
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                for v in decl.named_children:
                    name = v.child_by_field_name("name") if v.type == "variable_declarator" else None
                    if name is not None and name.type == "identifier":
                        declared.add(node_text(name, source))
            continue
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        spec_text = _specifier(node, source)
        if clause is None:
            if spec_text is not None and node_text(node, source).lstrip().startswith("export *") \
                    and " as " not in node_text(node, source):
                tgt = target(spec_text)
                if tgt is not None:
                    stars.append(tgt)
            continue
        tgt = target(spec_text) if spec_text is not None else None
        for spec in clause.named_children:
            name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
            if spec.type != "export_specifier" or name is None:
                continue
            local, exported = node_text(name, source), node_text(alias or name, source)
            if spec_text is not None:
                if tgt is not None:
                    renames.append((exported, tgt, local))
            elif local in imported:  # import { ping } from './ping'; export { ping };
                via = target(imported[local][0])
                if via is not None:
                    renames.append((exported, via, imported[local][1]))
            else:  # const x = …; export { x };
                declared.add(exported)
    if not declared and not renames and not stars and default is None:
        return None
    return ModuleExports(_stem(rel), frozenset(declared), tuple(renames), tuple(stars), default)


def declaring(modules: dict[str, ModuleExports], stem: str, name: str) -> tuple[str, str] | None:
    """The (stem, name) that declares export ``name`` of ``stem``, following relative
    re-exports; None when it cannot be told (unknown, ambiguous, too deep or a cycle)."""

    def follow(stem: str, name: str, hops: int) -> tuple[str, str] | None:
        mod = modules.get(stem)
        if mod is None or hops > _MAX_HOPS:
            return None
        if name in mod.declared:
            return stem, name
        if name == "default" and mod.default is not None:
            return stem, mod.default
        for exported, tgt, orig in mod.renames:
            if exported == name:
                return follow(tgt, orig, hops + 1)
        found = {r for tgt in mod.stars if (r := follow(tgt, name, hops + 1)) is not None}
        return found.pop() if len(found) == 1 else None

    return follow(stem, name, 0)
