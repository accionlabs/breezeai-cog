"""Angular route detection. Unlike Nest/Spring, Angular routes are **config objects**
(``{ path, component, loadChildren, canActivate, children }``) in ``Routes`` arrays /
``RouterModule.forRoot([...])``. Emits ``semanticType="route"`` / ``routeKind="page"`` statements
for the routes that show a component (``component`` / ``loadComponent``), parented to the file
(Angular routes are config, not handler methods). Nested ``children`` paths are joined onto the
parent path.

Wiring has no record (contract §4): a ``loadChildren`` lazy mount (its prefix and guards reach the
loaded routes: the repo mount index and the ``angular.mount-guards`` fact), a path-only parent
``{ path, children }``, and a layout — a component whose children include an empty-path child,
which always fills its outlet, so the parent's URL is that child's page."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import file_id
from ..emits import Emit
from ..treesitter import entry_text, node_text
from .mount_guards import incoming_guards
from .mounts import route_mounts

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
    its declared literal (cross-file, globally-unique); a ``+`` concatenation or a template
    literal whose **every** piece resolves, assembled in source order (``RouteNames.X + '/:' +
    RouteParams.Y`` -> ``org/:orgId``). Anything with an unresolved piece — a call, a variable
    not in the index, an ambiguous const — → ``None`` (honest-null, never a guessed segment)."""
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
    elif node.type == "binary_expression":
        return _resolve_concat(node, source, index)
    elif node.type == "template_string":
        return _resolve_template(node, source, index)
    return None


def _resolve_concat(node: Node, source: bytes, index: object) -> str | None:
    """A ``+`` string concatenation: resolve the left and right operands and join. Any operator
    other than ``+``, or an unresolved operand, → ``None`` (a single wrong operand must not
    yield a fabricated path). Left-nested ``a + b + c`` recurses down the left operand."""
    op = node.child_by_field_name("operator")
    if op is None or node_text(op, source) != "+":
        return None
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    lv = _resolve_path(left, source, index)
    rv = _resolve_path(right, source, index)
    if lv is None or rv is None:
        return None
    return lv + rv


def _resolve_template(node: Node, source: bytes, index: object) -> str | None:
    """A template literal: concatenate its ``string_fragment`` parts (verbatim) with each
    ``${…}`` substitution resolved via the index. Any substitution that does not resolve to a
    const literal → ``None`` (never stringify a dynamic expression)."""
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "string_fragment":
            parts.append(node_text(child, source))
        elif child.type == "template_substitution":
            inner = next((c for c in child.named_children), None)
            resolved = _resolve_path(inner, source, index)
            if resolved is None:
                return None
            parts.append(resolved)
        else:  # any other child (e.g. an escape) — be conservative
            return None
    return "".join(parts)


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


#: Route keys whose guards run before this route's own component (in source order).
_OWN_GUARD_KEYS = ("canActivate", "canMatch")


def _own_guards(elem: Node, source: bytes) -> list[str]:
    """``canActivate`` / ``canMatch`` guard names of one route object, in source order. Only
    named guards count — an inline functional guard has no name."""
    out: list[str] = []
    for pair in elem.named_children:
        if pair.type == "pair" and _key(pair, source) in _OWN_GUARD_KEYS:
            out.extend(_guards(pair.child_by_field_name("value"), source))
    return out


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


# Keys that only appear in Angular route config objects, never in breadcrumb/nav helpers.
_ROUTE_ONLY_KEYS = frozenset({
    "component", "loadChildren", "loadComponent",
    "canActivate", "canActivateChild", "canDeactivate", "canLoad", "canMatch",
    "outlet", "resolve", "pathMatch", "redirectTo",
    "children",  # breadcrumbs use 'children' too, but NEVER alongside component/load*
})
# A route array element must have 'path' PLUS at least one router-exclusive key so that
# plain {name, path, children} breadcrumb/nav objects are never captured.
_ROUTE_DISCRIMINATING_KEYS = frozenset({
    "component", "loadChildren", "loadComponent",
    "canActivate", "canActivateChild", "canDeactivate", "canLoad", "canMatch",
    "outlet", "resolve", "pathMatch", "redirectTo",
})


def _is_route_array(arr: Node, source: bytes) -> bool:
    for e in arr.named_children:
        if e.type != "object":
            continue
        keys = _pairs(e, source).keys()
        if "path" in keys and keys & _ROUTE_DISCRIMINATING_KEYS:
            return True
    return False


def _is_children_value(arr: Node, source: bytes) -> bool:
    p = arr.parent
    return p is not None and p.type == "pair" and _key(p, source) == "children"


def _has_empty_path_child(children: Node | None, source: bytes) -> bool:
    """Whether a ``children`` array holds a route object with ``path: ''``."""
    if children is None or children.type != "array":
        return False
    for child in children.named_children:
        if child.type == "object":
            p = _pairs(child, source).get("path")
            if p is not None and p.type == "string" and _string_val(p, source) == "":
                return True
    return False


def _process(arr: Node, prefix: str | None, source: bytes, path: str,
             routes: list[Emit], index: object, inherited: tuple[str, ...] = (),
             mounts: list[tuple[Node, list[str]]] | None = None) -> None:
    """``inherited``: guards of the ancestor routes that run before this route — each ancestor's
    own ``canMatch`` / ``canActivate`` (Angular matches and activates the parent first) and then
    its ``canActivateChild``, which guards every child binding, not the route that declares it.
    ``mounts`` (when given) collects each lazy mount's ``loadChildren`` node with the guards its
    loaded routes inherit — read by the ``angular.mount-guards`` fact."""
    for elem in arr.named_children:
        if elem.type != "object":
            continue
        pairs = _pairs(elem, source)
        if "path" not in pairs:
            continue
        if "redirectTo" in pairs:
            continue
        # Breadcrumb / nav-tree objects always carry a "name" key alongside "path" and
        # optional "children"; real Angular route config objects never have a top-level
        # "name" property — skip to avoid false-positive page nodes.
        if "name" in pairs:
            continue
        full = _compose(prefix, _resolve_path(pairs["path"], source, index))
        own = _own_guards(elem, source)
        load = pairs.get("loadChildren")  # lazy route group → wiring
        component = pairs.get("component")
        load_component = pairs.get("loadComponent")  # lazy standalone component → page
        children = pairs.get("children")
        child_guards = (*inherited, *own, *_guards(pairs.get("canActivateChild"), source))
        if load is not None:
            if mounts is not None:
                mounts.append((load, list(child_guards)))
        elif component is not None or load_component is not None:
            # A layout (a child with path '' always fills the outlet) is wiring; otherwise the
            # parent's own URL shows it, so it is a page.
            if not _has_empty_path_child(children, source):
                handler = (node_text(component, source) if component is not None
                           else _lazy_target(load_component, source))
                routes.append(Emit(elem, dict(
                    semanticType="route",
                    endpoint=full,
                    framework="angular",
                    routeKind="page",
                    handler=handler,
                    guards=[*inherited, *own] or None,
                ), parent=file_id(path), node_type="synthetic", text=entry_text(elem, source)))
        if children is not None and children.type == "array":
            _process(children, full, source, path, routes, index, child_guards, mounts)


_CLASS_NODE_TYPES = ("class_declaration", "class", "abstract_class_declaration")


def _mount_prefix(root: Node, source: bytes, index: object) -> str:
    """The lazy-mount prefix this file's routes hang under, from the repo mount index — the
    fully-composed parent prefix keyed by a class this file declares (a lazy child module is
    reached via ``.then(m => m.XModule)``). Empty string when this file is a routing root, its
    module isn't mounted, or the mount is ambiguous/unresolved (honest-null → own bare paths)."""
    mounts = route_mounts(index)
    if not mounts:
        return ""
    # Search all class node shapes (grammar may emit "class" or "class_declaration" depending
    # on context — cover both to avoid missing a module class in an export_statement).
    for cls_type in _CLASS_NODE_TYPES:
        for cls in _all(root, cls_type):
            name = cls.child_by_field_name("name")
            if name is not None:
                prefix = mounts.get(node_text(name, source))
                if prefix is not None:
                    return prefix
    return ""


_ROUTER_BYTE_GUARDS = (b"@angular/router", b"RouterModule", b"Routes")


def detect_angular_routes(root: Node, source: bytes, path: str, *,
                          index: object = None) -> list[Emit]:
    """This file's Angular page routes, for :func:`~..emits.apply`."""
    # Fast-path: files that don't import @angular/router (breadcrumb services, nav helpers,
    # etc.) can't contain Angular route arrays — skip the entire AST walk.
    if not any(guard in source for guard in _ROUTER_BYTE_GUARDS):
        return []
    routes: list[Emit] = []
    prefix = _mount_prefix(root, source, index)  # parent mount prefix (cross-file), else ""
    # Guards of the lazy mount that loads this file's routes (cross-file), else none.
    incoming = tuple(incoming_guards(index, path, declared_classes(root, source)))
    for arr in _all(root, "array"):
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process(arr, prefix, source, path, routes, index, incoming)
    return routes


def declared_classes(root: Node, source: bytes) -> list[str]:
    """Names of the classes this file declares (a lazily-loaded module is reached by one)."""
    return [node_text(name, source)
            for cls_type in _CLASS_NODE_TYPES for cls in _all(root, cls_type)
            if (name := cls.child_by_field_name("name")) is not None]


def lazy_mount_guards(root: Node, source: bytes) -> list[tuple[Node, list[str]]]:
    """Each lazy mount's ``loadChildren`` node with the guards its loaded routes inherit from
    this file (the mount route's ancestors, its own ``canActivate`` / ``canMatch`` and its
    ``canActivateChild``)."""
    if not any(guard in source for guard in _ROUTER_BYTE_GUARDS):
        return []
    found: list[tuple[Node, list[str]]] = []
    for arr in _all(root, "array"):
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process(arr, "", source, "", [], None, (), found)
    return found
