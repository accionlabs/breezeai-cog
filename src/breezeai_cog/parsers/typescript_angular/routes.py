"""Angular route detection. Unlike Nest/Spring, Angular routes are **config objects**
(``{ path, component, loadChildren, canActivate, children }``) in ``Routes`` arrays /
``RouterModule.forRoot([...])``. Emits ``semanticType="route"`` statements
(``routeKind="page"`` for component routes, ``"mount"`` for ``loadChildren`` lazy
mounts). Nested ``children`` paths are joined onto the parent path. Routes are parented
to the file (Angular routes are config, not handler methods)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text


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


def _process(arr: Node, prefix: str, source: bytes, path: str, seen: set[str], routes: list[Statement]) -> None:
    for elem in arr.named_children:
        if elem.type != "object":
            continue
        pairs = _pairs(elem, source)
        if "path" not in pairs:
            continue
        full = _join(prefix, _string_val(pairs["path"], source))
        load = pairs.get("loadChildren")
        component = pairs.get("component")
        sl, sc = elem.start_point[0] + 1, elem.start_point[1]
        routes.append(Statement(
            id=disambiguate(statement_id(path, sl, sc), seen),
            parentId=file_id(path),
            nodeType="object",
            semanticType="route",
            text=node_text(elem, source).split("\n", 1)[0][:120],
            endpoint=full,
            framework="angular",
            routeKind="mount" if load is not None else "page",
            handler=node_text(component, source) if component is not None else None,
            guards=_guards(pairs.get("canActivate"), source) or None,
            startLine=sl,
            endLine=elem.end_point[0] + 1,
            path=path,
        ))
        children = pairs.get("children")
        if children is not None and children.type == "array":
            _process(children, full, source, path, seen, routes)


def detect_angular_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    routes: list[Statement] = []
    for arr in _all(root, "array"):
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process(arr, "", source, path, seen_ids, routes)
    return routes
