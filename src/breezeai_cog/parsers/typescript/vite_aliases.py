"""Vite ``resolve.alias`` as import-resolution scopes.

A Vite project writes its ``@`` alias in ``vite.config.*``, not in a ``tsconfig``::

    export default defineConfig({
      resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    });

This module reads those entries into the scope the ``tsconfig`` ``paths`` aliases already use,
``(config dir, {pattern: [absolute targets]})``, so ``@/views/Home`` resolves like any aliased
import. Vite matches a string key against the import itself or ``key/…``, so ``'@'`` gives two
patterns: ``@`` and ``@/*``.

Only what the source states literally is read. Anything else is left alone and the import stays
external (honest-null):

==========================================  =================================================
Entry                                       Read as
==========================================  =================================================
``'@': fileURLToPath(new URL('./src',       the config's folder + ``./src``
import.meta.url))``
``'@': path.resolve(__dirname, 'src')``     the config's folder + ``src``; ``resolve(…)`` and
                                            ``import.meta.dirname`` read the same way
``[{ find: '@', replacement: … }]``         as above; a RegExp ``find`` is skipped
a variable, a template, any other call, a   skipped (Vite uses a relative string as-is, it is
relative or absolute string                 not a folder in the repo)
==========================================  =================================================

The config is what ``export default`` or ``module.exports =`` gives: an object, or
``defineConfig({…})`` around one (``satisfies``, ``as`` and parentheses around either are looked
through). A target outside the repository, or inside ``node_modules``, is dropped.
"""

from __future__ import annotations

import os
from pathlib import Path

from tree_sitter import Node

from ...logging import get_logger
from ..treesitter import node_text, parse_source
from .imports import _pair_key_name, _string_literal, _unwrap_const_expr

AliasScope = tuple[str, dict[str, list[str]]]

#: What a Vite config calls its own folder: ``__dirname`` (CommonJS / shimmed) or
#: ``import.meta.dirname``.
_DIRNAMES = ("__dirname", "import.meta.dirname")


def _member(obj: Node | None, name: str, source: bytes) -> Node | None:
    """The value of property ``name`` of an object literal."""
    if obj is None or obj.type != "object":
        return None
    for pair in obj.named_children:
        if pair.type == "pair" and _pair_key_name(pair.child_by_field_name("key"), source) == name:
            return pair.child_by_field_name("value")
    return None


def _exported_value(top: Node, source: bytes) -> Node | None:
    """The config a top-level statement exports: ``export default X`` or ``module.exports = X``."""
    if top.type == "export_statement" and any(c.type == "default" for c in top.children):
        return top.child_by_field_name("value")
    assign = next((c for c in top.named_children if c.type == "assignment_expression"), None)
    if top.type == "expression_statement" and assign is not None:
        left = assign.child_by_field_name("left")
        if left is not None and node_text(left, source) == "module.exports":
            return assign.child_by_field_name("right")
    return None


def _config_object(root: Node, source: bytes) -> Node | None:
    """The config object: the exported ``{…}``, or the one ``defineConfig({…})`` wraps, either
    possibly written ``{…} satisfies UserConfig`` / ``{…} as UserConfig`` / ``({…})``."""
    for top in root.named_children:
        value = _unwrap_const_expr(_exported_value(top, source))
        if value is not None and value.type == "call_expression":
            fn = value.child_by_field_name("function")
            args = value.child_by_field_name("arguments")
            if fn is None or args is None or node_text(fn, source) != "defineConfig":
                continue
            value = _unwrap_const_expr(args.named_children[0] if args.named_children else None)
        if value is not None and value.type == "object":
            return value
    return None


def _entries(alias: Node | None, source: bytes) -> list[tuple[str, Node]]:
    """(find, replacement) of each alias entry whose ``find`` is a string: the object form
    (``{ '@': … }``) or the array form (``[{ find: '@', replacement: … }]``)."""
    out: list[tuple[str, Node]] = []
    if alias is None:
        return out
    if alias.type == "object":
        for pair in alias.named_children:
            if pair.type != "pair":
                continue
            find = _pair_key_name(pair.child_by_field_name("key"), source)
            value = pair.child_by_field_name("value")
            if find is not None and value is not None:
                out.append((find, value))
    elif alias.type == "array":
        for item in alias.named_children:
            find = _string_literal(_member(item, "find", source), source)
            value = _member(item, "replacement", source)
            if find is not None and value is not None:
                out.append((find, value))
    return out


def _join(config_dir: Path, segments: list[str | None]) -> Path | None:
    """``config_dir`` + the segments, or None unless each is a relative string literal (an
    absolute one would restart the path)."""
    literals = [s for s in segments if s is not None and not s.startswith("/")]
    if len(literals) != len(segments):
        return None
    return Path(os.path.normpath(config_dir.joinpath(*literals)))


def _url_target(args: list[Node], config_dir: Path, source: bytes) -> Path | None:
    """``fileURLToPath(new URL('./src', import.meta.url))``."""
    if len(args) != 1 or args[0].type != "new_expression":
        return None
    ctor = args[0].child_by_field_name("constructor")
    url_args = args[0].child_by_field_name("arguments")
    if ctor is None or url_args is None or node_text(ctor, source) != "URL":
        return None
    parts = url_args.named_children
    if len(parts) != 2 or node_text(parts[1], source) != "import.meta.url":
        return None
    return _join(config_dir, [_string_literal(parts[0], source)])


def _resolve_target(args: list[Node], config_dir: Path, source: bytes) -> Path | None:
    """``path.resolve(__dirname, 'src', …)``."""
    if len(args) < 2 or node_text(args[0], source) not in _DIRNAMES:
        return None
    return _join(config_dir, [_string_literal(a, source) for a in args[1:]])


def _target(node: Node, config_dir: Path, source: bytes) -> Path | None:
    """The folder a replacement names, in the two forms read; None for anything else."""
    fn = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if node.type != "call_expression" or fn is None or args is None:
        return None
    name = node_text(fn, source)
    if name == "fileURLToPath":
        return _url_target(args.named_children, config_dir, source)
    if name in ("path.resolve", "resolve"):
        return _resolve_target(args.named_children, config_dir, source)
    return None


def _is_repo_source(path: Path, repo_root: Path) -> bool:
    """Inside the repo and outside ``node_modules``, whose files are never captured (an edge to
    one would point at no node)."""
    return (
        path.is_relative_to(repo_root) and "node_modules" not in path.relative_to(repo_root).parts
    )


def _patterns(
    entries: list[tuple[str, Node]], config_dir: Path, repo_root: Path, source: bytes
) -> dict[str, list[str]]:
    """``find`` → target and ``find/*`` → ``target/*`` for each entry that names a folder of the
    repo's own sources; the ``tsconfig`` ``paths`` shape, absolute targets."""
    patterns: dict[str, list[str]] = {}
    for find, node in entries:
        if not find or find.endswith("/") or "*" in find:
            continue
        target = _target(node, config_dir, source)
        resolved = target.resolve() if target is not None else None
        if resolved is None or not _is_repo_source(resolved, repo_root.resolve()):
            continue
        patterns.setdefault(find, []).append(str(resolved))
        patterns.setdefault(f"{find}/*", []).append(f"{resolved}/*")
    return patterns


def vite_alias_scope(cfg: Path, repo_root: Path) -> AliasScope | None:
    """One ``vite.config.*`` as an alias scope, or None when it declares no alias that can be
    read. A config that can't be read or parsed is skipped with a warning, never aborts indexing."""
    try:
        source = cfg.read_bytes()
        if b"alias" not in source:
            return None
        root = parse_source("typescript", source, 0).root_node
        alias = _member(_member(_config_object(root, source), "resolve", source), "alias", source)
        config_dir = cfg.parent.resolve()
        patterns = _patterns(_entries(alias, source), config_dir, repo_root, source)
    except Exception as exc:  # an unreadable file (OSError) or a parser failure; skip this config
        get_logger("breezeai_cog.index").warning(
            "vite.alias.skipped",
            path=str(cfg),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return (str(config_dir), patterns) if patterns else None
