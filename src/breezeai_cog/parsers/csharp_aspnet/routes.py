"""ASP.NET route detection.

Two idioms, both emitted as ``route`` statements (spec A4/C5):

* **Controllers — off the record.** The C#/VB base parser already captured
  ``[ApiController]`` / ``[Route]`` / ``[HttpGet]`` onto ``Class.decorators`` /
  ``Function.decorators`` / ``Parameter.decorators``, so :func:`detect_controller_routes`
  reads the ``FileRecord`` directly (no AST re-walk). Because it only touches the
  language-agnostic record shape, the VB ASP.NET parser reuses it verbatim.
* **Minimal APIs — AST walk.** ``app.MapGet("/x", …)`` is call-based, so
  :func:`detect_minimal_api_routes` walks the tree for the ``MapGet``/``MapPost``/… calls
  (grammar-specific node names are passed in by the C#/VB caller).
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Function, Statement
from ..treesitter import node_text

_HTTP_ATTRS = {
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT",
    "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD", "HttpOptions": "OPTIONS",
}
_CONTROLLER_ATTRS = {"ApiController", "Controller"}
_CONTROLLER_BASES = {"Controller", "ControllerBase"}
_MAP_METHODS = {
    "MapGet": "GET", "MapPost": "POST", "MapPut": "PUT",
    "MapDelete": "DELETE", "MapPatch": "PATCH",
}


def simple_attr_name(name: str) -> str:
    """Normalize a C#/VB attribute name to its short form: an attribute may be written
    ``[HttpGet]`` or ``[HttpGetAttribute]`` (and svcutil emits the full form) — both bind to
    the same class, so drop a trailing ``Attribute``."""
    return name[: -len("Attribute")] if name.endswith("Attribute") and name != "Attribute" else name


def _first_arg(dec: Decorator) -> str:
    """First positional (non-named) attribute arg — the route template, if any."""
    for raw in dec.args:
        s = raw.strip()
        if "=" not in s or s.lstrip().startswith('"'):
            return s.strip('"')
    return ""


def _controller_token(base: str, class_name: str) -> str:
    """Expand the ``[controller]`` route token to the controller name sans suffix."""
    name = class_name[:-len("Controller")] if class_name.endswith("Controller") else class_name
    return base.replace("[controller]", name).replace("{controller}", name)


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _convention_endpoint(class_name: str, action: str) -> str:
    """ASP.NET MVC **convention** route, used when no attribute resolves the path (classic
    MVC 5 routing declares ``{controller}/{action}`` in RouteConfig, not per action):
    ``/{controller}/{action}`` with the ``Controller`` suffix dropped
    (``CatalogController.Create`` → ``/Catalog/Create``)."""
    ctrl = class_name[: -len("Controller")] if class_name.endswith("Controller") else class_name
    return f"/{ctrl}/{action}"


def _class_route(decorators: list[Decorator], class_name: str) -> str:
    for d in decorators:
        if simple_attr_name(d.name) in ("Route", "RoutePrefix"):
            return _controller_token(_first_arg(d), class_name)
    return ""


def _response_dto(return_type: str | None) -> str | None:
    """Unwrap ``Task<…>`` / ``ActionResult<…>`` / ``Task(Of …)`` to the payload type;
    bare action results (``IActionResult``/``ActionResult``/``void``) → None."""
    if not return_type:
        return None
    t = return_type.strip()
    # C# generic: Foo<Bar> → Bar
    while "<" in t and t.endswith(">"):
        t = t[t.index("<") + 1: -1].strip()
    # VB generic: Foo(Of Bar) → Bar
    while t.startswith(("Task(Of ", "ValueTask(Of ", "ActionResult(Of ")) and t.endswith(")"):
        t = t[t.index("(Of ") + 4: -1].strip()
    if t in ("IActionResult", "ActionResult", "void", "Void", "Task", "ValueTask", ""):
        return None
    return t


def _request_dto(fn: Function) -> str | None:
    for p in fn.params:
        if any(simple_attr_name(d.name) == "FromBody" for d in p.decorators):
            return p.type or None
    return None


def _auth(decorators: list[Decorator]) -> tuple[bool, list[str]]:
    guards = [n for d in decorators if (n := simple_attr_name(d.name)) in ("Authorize", "AllowAnonymous")]
    return ("Authorize" in guards), guards


def _is_controller(cls) -> bool:
    if {simple_attr_name(d.name) for d in cls.decorators} & _CONTROLLER_ATTRS:
        return True
    ext = (cls.extends or "").rsplit(".", 1)[-1]
    return ext in _CONTROLLER_BASES or ext.endswith(("Controller", "ControllerBase"))


def detect_controller_routes(record: FileRecord) -> list[Statement]:
    controllers: dict[str, tuple[str, list[Decorator], str]] = {}
    for cls in record.classes:
        if _is_controller(cls):
            controllers[cls.id] = (_class_route(cls.decorators, cls.name), cls.decorators, cls.name)
    if not controllers:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        info = controllers.get(fn.parentId)
        if info is None:
            continue
        base, cls_decorators, cls_name = info
        cls_auth, cls_guards = _auth(cls_decorators)
        for dec in fn.decorators:
            verb = _HTTP_ATTRS.get(simple_attr_name(dec.name))
            if verb is None and simple_attr_name(dec.name) in ("Route",) and any(
                    simple_attr_name(d.name) in _HTTP_ATTRS for d in fn.decorators):
                continue  # a bare [Route] alongside an [HttpX] — the verb comes from [HttpX]
            if verb is None:
                continue
            sub = _first_arg(dec)
            # attribute route when one is present; else the MVC convention /{controller}/{action}
            endpoint = _join(base, sub) if (base or sub) else _convention_endpoint(cls_name, fn.name)
            fn_auth, fn_guards = _auth(fn.decorators)
            routes.append(Statement(
                id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                parentId=fn.id,
                nodeType="synthetic",
                semanticType="route",
                text=f"[{dec.name}]",
                method=verb,
                endpoint=endpoint,
                framework="aspnet",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="route",
                isRegex=False,
                authRequired=(cls_auth or fn_auth) or None,
                guards=(cls_guards + fn_guards) or None,
                requestDTO=_request_dto(fn),
                responseDTO=_response_dto(fn.returnType),
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            ))
    return routes


def detect_minimal_api_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
    *,
    invocation_type: str,
    member_type: str,
) -> list[Statement]:
    """AST-walk for ``app.MapGet("/x", handler)`` minimal-API endpoints. Grammar-specific
    node/field names are supplied by the C#/VB caller."""
    routes: list[Statement] = []
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    name_node = func.child_by_field_name(name_field)
                    method = node_text(name_node, source) if name_node is not None else ""
                    verb = _MAP_METHODS.get(method)
                    if verb is not None:
                        endpoint = _minimal_path(child, source)
                        start = child.start_point[0] + 1
                        routes.append(Statement(
                            id=disambiguate(statement_id(path, start, child.start_point[1]), seen_ids),
                            parentId=path,
                            nodeType=child.type,
                            semanticType="route",
                            text=node_text(child, source).split("\n", 1)[0][:200],
                            method=verb,
                            endpoint=endpoint or "/",
                            framework="aspnet",
                            routeKind="route",
                            isRegex=False,
                            startLine=start,
                            endLine=child.end_point[0] + 1,
                            path=path,
                        ))
            visit(child)

    visit(root)
    return routes


def _minimal_path(call: Node, source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        lit = _find_string(arg)
        if lit is not None:
            return node_text(lit, source).strip('"')
    return None


def _find_string(node: Node) -> Node | None:
    if node.type == "string_literal":
        return node
    for c in node.named_children:
        found = _find_string(c)
        if found is not None:
            return found
    return None


_MAP_ROUTE_METHODS = {"MapRoute", "MapHttpRoute"}
_CTRL_RE = re.compile(r'controller\s*=\s*"([^"]+)"')
_ACTION_RE = re.compile(r'action\s*=\s*"([^"]+)"')


def _route_template(call: Node, source: bytes) -> str | None:
    """The URL-template arg of a ``MapRoute`` call — the string literal that looks like a
    route template (contains ``{`` or ``/``), skipping the leading route-name arg."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    strings = [node_text(s, source).strip('"') for arg in args.named_children
               if (s := _find_string(arg)) is not None]
    for s in strings:
        if "{" in s or "/" in s:
            return s
    return strings[1] if len(strings) >= 2 else None


def detect_route_registrations(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
    *,
    invocation_type: str,
    member_type: str,
) -> list[Statement]:
    """AST-walk for MVC / Web-API **convention-route registrations** —
    ``routes.MapRoute(name, url, defaults)`` / ``config.Routes.MapHttpRoute(…)`` declared in
    ``RouteConfig`` / ``Global.asax`` / an ``AreaRegistration``. Emits the declared URL
    template as a ``route`` (method ``ANY``, since a convention route matches any verb), with
    the default ``controller``[.``action``] as the handler. Grammar-specific node/field names
    are supplied by the C#/VB caller (Phase 2 of the MVC endpoint-resolution work)."""
    routes: list[Statement] = []
    name_field = "name" if member_type == "member_access_expression" else "member"
    fn_field = "function" if invocation_type == "invocation_expression" else "target"

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == invocation_type:
                func = child.child_by_field_name(fn_field)
                if func is not None and func.type == member_type:
                    name_node = func.child_by_field_name(name_field)
                    method = node_text(name_node, source) if name_node is not None else ""
                    url = _route_template(child, source) if method in _MAP_ROUTE_METHODS else None
                    if url:
                        text = node_text(child, source)
                        ctrl, action = _CTRL_RE.search(text), _ACTION_RE.search(text)
                        handler = (f"{ctrl.group(1)}.{action.group(1)}" if ctrl and action
                                   else ctrl.group(1) if ctrl else None)
                        start = child.start_point[0] + 1
                        routes.append(Statement(
                            id=disambiguate(statement_id(path, start, child.start_point[1]), seen_ids),
                            parentId=path,
                            nodeType=child.type,
                            semanticType="route",
                            text=text.split("\n", 1)[0][:200],
                            method="ANY",
                            endpoint="/" + url.strip("/"),
                            framework="aspnet",
                            handler=handler,
                            routeKind="route",
                            isRegex=False,
                            startLine=start,
                            endLine=child.end_point[0] + 1,
                            path=path,
                        ))
            visit(child)

    visit(root)
    return routes
