"""Angular route detection. Unlike Nest/Spring, Angular routes are **config objects**
(``{ path, component, loadChildren, canActivate, children }``) in ``Routes`` arrays /
``RouterModule.forRoot([...])``. Emits ``semanticType="route"`` statements
(``routeKind="page"`` for component routes, ``"mount"`` for ``loadChildren`` lazy
mounts). Nested ``children`` paths are joined onto the parent path. Routes are parented
to the file (Angular routes are config, not handler methods)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text

# Lazy-load target extraction (Tier 1): capture what a route loads so the mount is a
# traversable edge, not a dead end. Covers every Angular version's syntax:
#   () => import('./x').then(m => m.XModule)   NgModule / routes-const / component
#   () => import('./x.routes')                 default-export routes (fallback: the path)
#   'app/x/x.module#XModule'                   legacy string form (Angular <9)
_LAZY_STRING_RE = re.compile(r"""^\s*['"][^'"#]+#([A-Za-z_$][\w$]*)['"]\s*$""")
_LAZY_MEMBER_RE = re.compile(r"=>\s*[\w$]+\.([A-Za-z_$][\w$]*)")
_LAZY_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]""")


def _lazy_target(node: Node | None, source: bytes) -> str | None:
    """The module/routes/component a ``loadChildren`` / ``loadComponent`` resolves to —
    the class/const name when available (a resolvable graph edge), else the import path."""
    if node is None:
        return None
    text = node_text(node, source)
    m = _LAZY_STRING_RE.search(text)  # legacy 'path#Module' — most specific
    if m:
        return m.group(1)
    m = _LAZY_MEMBER_RE.search(text)  # .then(m => m.X) — module / routes const / component
    if m:
        return m.group(1)
    m = _LAZY_IMPORT_RE.search(text)  # default-export import — fall back to the path
    return m.group(1) if m else None


def _all(node: Node, typ: str) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == typ:
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(node)
    return out


def _key(pair: Node, source: bytes) -> str | None:
    k = pair.child_by_field_name("key")
    return node_text(k, source).strip("'\"") if k is not None else None


def _pairs(obj: Node, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for c in obj.named_children:
        if c.type == "pair":
            key = _key(c, source)
            value = c.child_by_field_name("value")
            if key and value is not None:
                out[key] = value
    return out


def _string_val(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    return node_text(node, source)


def _resolve_path(node: Node | None, source: bytes, index: object) -> str | None:
    """The route ``path`` as a literal string, resolving non-literal forms via the repo
    value index: a string literal verbatim; ``RouteNames.X`` / a bare ``CONST`` resolved to
    its declared literal (cross-file, globally-unique); anything else (template, call,
    concatenation, ambiguous) → ``None`` (honest-null — never the raw symbol text)."""
    if node is None:
        return None
    if node.type == "string":
        return _string_val(node, source)  # literal path (may be "")
    values = getattr(index, "const_values", None) or {}
    if node.type == "member_expression":
        obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
        if obj is not None and prop is not None:
            return values.get(f"{node_text(obj, source)}.{node_text(prop, source)}")
    elif node.type == "identifier":
        return values.get(node_text(node, source))
    return None


def _compose(prefix: str | None, sub: str | None) -> str | None:
    """Join a parent prefix and a route sub-path; if either is unresolved (None), the full
    path is unknown → None (a relative segment needs its prefix)."""
    if prefix is None or sub is None:
        return None
    return _join(prefix, sub)


def _guards(node: Node | None, source: bytes) -> list[str]:
    if node is None or node.type != "array":
        return []
    return [node_text(c, source) for c in node.named_children if c.type == "identifier"]


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _is_route_array(arr: Node, source: bytes) -> bool:
    return any(e.type == "object" and "path" in _pairs(e, source) for e in arr.named_children)


def _is_children_value(arr: Node, source: bytes) -> bool:
    p = arr.parent
    return p is not None and p.type == "pair" and _key(p, source) == "children"


def _process(arr: Node, prefix: str | None, source: bytes, path: str, seen: set[str],
             routes: list[Statement], index: object) -> None:
    for elem in arr.named_children:
        if elem.type != "object":
            continue
        pairs = _pairs(elem, source)
        if "path" not in pairs:
            continue
        full = _compose(prefix, _resolve_path(pairs["path"], source, index))
        load = pairs.get("loadChildren")  # lazy route group -> mount
        component = pairs.get("component")
        load_component = pairs.get("loadComponent")  # lazy standalone component -> page
        if load is not None:  # mount: handler = the loaded module/routes (a traversable link)
            handler = _lazy_target(load, source)
        elif component is not None:
            handler = node_text(component, source)
        elif load_component is not None:
            handler = _lazy_target(load_component, source)
        else:
            handler = None
        sl, sc = elem.start_point[0] + 1, elem.start_point[1]
        routes.append(Statement(
            id=disambiguate(statement_id(path, sl, sc), seen),
            parentId=file_id(path),
            nodeType="synthetic",
            semanticType="route",
            text=node_text(elem, source).split("\n", 1)[0][:120],
            endpoint=full,
            framework="angular",
            routeKind="mount" if load is not None else "page",
            handler=handler,
            guards=_guards(pairs.get("canActivate"), source) or None,
            startLine=sl,
            endLine=elem.end_point[0] + 1,
            path=path,
        ))
        children = pairs.get("children")
        if children is not None and children.type == "array":
            _process(children, full, source, path, seen, routes, index)


def detect_angular_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str],
                          index: object = None) -> list[Statement]:
    routes: list[Statement] = []
    for arr in _all(root, "array"):
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process(arr, "", source, path, seen_ids, routes, index)
    return routes
