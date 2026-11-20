"""Application base paths — the shared part of the ``next.base-paths`` and
``loopback.base-paths`` facts (each framework registers its own, with its own gate).

An application can put one path in front of every route it serves::

    // apps/web/next.config.js                  → {"apps/web": "/web"}
    module.exports = { basePath: '/web' };
    // src/application.ts (LoopBack)            → {"src": "/api"}
    super({ rest: { basePath: '/api' } });      // or: this.basePath('/api')

The fact maps the directory of the file that sets it (the app directory) to the base path:
``""`` when the app sets none, a template with a ``${name}`` part when it is set from a value
that does not resolve (``${BASE_PATH}`` for ``process.env.BASE_PATH``, see ``addresses``),
``None`` when values disagree within one app directory. A route belongs to the deepest app directory above it
(:func:`app_base_path`). Pieces may name constants of other files, so they resolve against the
index's constant map once it is complete.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Literal

from tree_sitter import Node

from .addresses import runtime_part

from .treesitter import node_text
from .typescript.imports import _path_pieces, piece_name, piece_value
from .typescript.scopes import ConstView, collect_consts

#: One file's base paths: (its directory, the path pieces of each ``basePath`` it sets —
#: a dynamic value is one ``("rt", source text)`` piece; an empty list = a Next.js config that
#: sets none).
BasePathRaw = tuple[str, list[list[tuple[str, str]]]]


def collect_base_path(
    root: Node, source: bytes, rel: str, framework: Literal["nextjs", "loopback"],
    repo_root: Path | None = None,
) -> BasePathRaw | None:
    """The base paths this file sets. Next.js: a ``basePath`` key in any object of a
    ``next.config.*`` file (``module.exports = {…}``, ``export default withX({…})``) — the caller
    passes only config files. LoopBack: a ``basePath`` key inside a ``rest: {…}`` options object,
    or an ``x.basePath('/api')`` call. None when a LoopBack file sets none."""
    found: list[list[tuple[str, str]]] = []
    view = collect_consts(root, source, rel, repo_root)  # which constant each name reads here

    def walk(n: Node) -> None:
        if n.type == "pair":
            key = n.child_by_field_name("key")
            value = n.child_by_field_name("value")
            if key is not None and value is not None and node_text(key, source).strip("'\"") == "basePath":
                outer = n.parent.parent if n.parent is not None else None
                okey = outer.child_by_field_name("key") if outer is not None and outer.type == "pair" else None
                if framework == "nextjs" or (
                        okey is not None and node_text(okey, source).strip("'\"") == "rest"):
                    found.append(_pieces(value, source, view))
        elif n.type == "call_expression" and framework == "loopback":
            fn = n.child_by_field_name("function")
            prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else None
            args = n.child_by_field_name("arguments")
            arg_nodes = list(args.named_children) if args is not None else []
            if prop is not None and node_text(prop, source) == "basePath" and len(arg_nodes) == 1:
                found.append(_pieces(arg_nodes[0], source, view))
        for c in n.named_children:
            walk(c)

    walk(root)
    if framework == "loopback" and not found:
        return None
    anchor = PurePosixPath(rel).parent.as_posix()
    return ("" if anchor == "." else anchor), found


def resolve_base_paths(values: list[BasePathRaw], index: Any) -> dict[str, str | None] | None:
    """App directory → base path (see the module docstring), or None when no app sets one."""
    consts: dict[str, str | None] = getattr(index, "const_values", None) or {}
    by_dir: dict[str, set[str | None]] = {}
    for anchor, found in values:
        bucket = by_dir.setdefault(anchor, set())
        for pieces in found:
            bucket.add(_template(pieces, consts))
    out = {anchor: "" if not vals else next(iter(vals)) if len(vals) == 1 else None
           for anchor, vals in by_dir.items()}
    return out or None


def app_base_path(apps: dict[str, str | None], path: str) -> tuple[bool, str | None]:
    """(found, base path) of the app a ``path`` belongs to: the deepest app directory above it.
    ``found`` False → no app config seen (address unchanged); base ``None`` → values disagree
    (the address is unknown); a base with a ``${name}`` part → set from a runtime value."""
    anchors = [d for d in apps if d == "" or path.startswith(d + "/")]
    if not anchors:
        return False, None
    return True, apps[max(anchors, key=len)]


def _pieces(node: Node, source: bytes, view: ConstView) -> list[tuple[str, str]]:
    """A ``basePath`` value's pieces; a value with a dynamic piece (a call, a condition) is one
    ``("rt", source text)`` piece, named when the base is resolved."""
    pieces = _path_pieces(node, source, view)
    return pieces if pieces is not None else [("rt", node_text(node, source))]


def _template(pieces: list[tuple[str, str]], consts: dict[str, str | None]) -> str:
    """The base path the pieces give: literals as written, a constant by its value, and a value
    read at runtime (a dynamic piece, or a name no constant resolves) as ``${name}``."""
    out: list[str] = []
    for kind, text in pieces:
        if kind == "lit":
            out.append(text)
        elif (value := piece_value(kind, text, consts)) is not None:
            out.append(value)
        else:
            out.append(runtime_part(piece_name(kind, text)))
    return "".join(out)
