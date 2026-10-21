"""ASP.NET MVC areas: where an area controller's actions are served.

What puts a controller in an area is code, never its folder:

==============================  ==========================================  =====================
Declared by                     Applies to                                  Prefix
==============================  ==========================================  =====================
``[RouteArea("Blog")]``         MVC 5 attribute routes of the controller    ``AreaPrefix =``, else
(``AreaPrefix = "admin/blog"``)                                             the area name
``AreaRegistration``            MVC 5 convention routes of controllers in   the template:
``context.MapRoute(…, "Tests/   its namespaces (default: the registration's ``/Tests/{Ctrl}/{Act}``
{controller}/{action}/{id}"…)`` namespace and those below it)
``[Area("Admin")]``             ASP.NET Core: the ``[area]`` token of       ``Admin`` for
                                attribute templates                         ``[area]``
==============================  ==========================================  =====================

A controller under an ``Areas/`` folder whose area none of these confirms has an unknown
conventional address (empty endpoint), never ``/{Controller}/{Action}`` as if it were not in an
area, and never the folder name as a guessed prefix. Two facts on the C# index feed this:
``csharp.string-constants`` (``const string`` fields, so ``[RouteArea(CmsConstants.Areas.Blog)]``
resolves) and ``aspnet.area-registrations``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node

from .additive import FactSpec, index_fact, register_fact
from .index_common import record_distinct
from .treesitter import node_text

CONSTANTS = "csharp.string-constants"
REGISTRATIONS = "aspnet.area-registrations"

_CLASSES = ("class_declaration", "struct_declaration", "record_declaration")
_NAMESPACES = ("namespace_declaration", "file_scoped_namespace_declaration")


def _string(node: Node | None, source: bytes) -> str | None:
    """A plain C# string literal's value (no interpolation), else None."""
    if node is None or node.type not in ("string_literal", "verbatim_string_literal"):
        return None
    content = next((c for c in node.named_children if c.type == "string_literal_content"), None)
    if node.type == "verbatim_string_literal":
        return node_text(node, source)[2:-1]
    return node_text(content, source) if content is not None else ""


def _name(node: Node, source: bytes) -> str | None:
    nm = node.child_by_field_name("name")
    return node_text(nm, source) if nm is not None else None


# ---- csharp.string-constants -------------------------------------------------------------

def _collect_constants(root: Node, source: bytes, rel: str,
                       repo_root: Path | None) -> list[tuple[str, str]] | None:
    """``Namespace.Outer.Inner.NAME`` → value for each ``const string`` / ``static readonly
    string`` field with a literal value."""
    found: list[tuple[str, str]] = []

    def walk(node: Node, chain: tuple[str, ...]) -> None:
        for c in node.named_children:
            if c.type in _CLASSES:
                nm = _name(c, source)
                body = c.child_by_field_name("body")
                if nm and body is not None:
                    # A top-level class is keyed with its namespace; a nested one by its chain.
                    outer = chain or tuple(p for p in [namespace_of(c, source)] if p)
                    walk(body, (*outer, nm))
            elif c.type == "field_declaration" and chain and node.type == "declaration_list" \
                    and node.parent is not None and node.parent.type in _CLASSES:
                mods = {node_text(m, source) for m in c.named_children if m.type == "modifier"}
                if "const" not in mods and not {"static", "readonly"} <= mods:
                    continue
                decl = next((d for d in c.named_children if d.type == "variable_declaration"), None)
                for v in decl.named_children if decl is not None else []:
                    if v.type != "variable_declarator":
                        continue
                    ident = next((x for x in v.named_children if x.type == "identifier"), None)
                    value = _string(next((x for x in v.named_children if x.type.endswith(
                        "string_literal")), None), source)
                    if ident is not None and value is not None:
                        found.append((".".join((*chain, node_text(ident, source))), value))
            elif c.type in _NAMESPACES or c.type == "declaration_list" or c.type.startswith("preproc_"):
                body = c.child_by_field_name("body") if c.type in _NAMESPACES else c
                walk(body if body is not None else c, chain)

    walk(root, ())
    return found or None


def _resolve_constants(values: list[list[tuple[str, str]]], index: Any) -> dict[str, str | None] | None:
    out: dict[str, str | None] = {}
    for file_values in values:
        for key, value in file_values:
            record_distinct(out, key, value)
    return out or None


register_fact(FactSpec(name=CONSTANTS, language="csharp", gate=(b"const ", b"readonly "),
                       collect=_collect_constants, resolve=_resolve_constants))


def string_value(node: Node | None, source: bytes, index: Any | None,
                 namespace: str = "") -> str | None:
    """An attribute argument's string: a literal, or a constant (``CmsConstants.Areas.Blog``).
    A constant is looked up the way C# binds it: in the referencing code's namespace and each
    enclosing one, then as written (fully qualified); failing those (a ``using`` brought it in),
    the one constant of the repo with that class chain. Unknown or ambiguous → None."""
    if node is None:
        return None
    literal = _string(node, source)
    if literal is not None:
        return literal
    if node.type not in ("member_access_expression", "identifier", "qualified_name"):
        return None
    consts: dict[str, str | None] = index_fact(index, CONSTANTS) or {}
    ref = node_text(node, source).replace(" ", "")
    parts = namespace.split(".") if namespace else []
    for i in range(len(parts), -1, -1):  # innermost namespace out, then as written
        key = ".".join([*parts[:i], ref])
        if key in consts:
            return consts[key]  # None when that key is ambiguous
    hits = {v for k, v in consts.items() if k.endswith("." + ref)}
    return next(iter(hits)) if len(hits) == 1 else None


# ---- aspnet.area-registrations -----------------------------------------------------------

#: One registration route: (the namespaces its controllers are in, each with ``True`` when the
#: namespaces below it count too; the URL template).
Registration = tuple[tuple[tuple[str, bool], ...], str]


def namespace_of(node: Node, source: bytes) -> str:
    """The namespace a declaration sits in (nested ``namespace`` blocks joined)."""
    parts: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type in _NAMESPACES:
            nm = parent.child_by_field_name("name")
            if nm is not None:
                parts.insert(0, node_text(nm, source))
        parent = parent.parent
    if not parts:  # a file-scoped namespace is a sibling before the class, not an ancestor
        root = node
        while root.parent is not None:
            root = root.parent
        fs = next((c for c in root.named_children
                   if c.type == "file_scoped_namespace_declaration"), None)
        nm = fs.child_by_field_name("name") if fs is not None else None
        if nm is not None:
            parts.append(node_text(nm, source))
    return ".".join(parts)


def _collect_registrations(root: Node, source: bytes, rel: str,
                           repo_root: Path | None) -> list[Registration] | None:
    found: list[Registration] = []

    def walk(node: Node) -> None:
        if node.type in _CLASSES:
            bases = next((c for c in node.named_children if c.type == "base_list"), None)
            names = {node_text(b, source).rsplit(".", 1)[-1] for b in
                     (bases.named_children if bases is not None else [])}
            if "AreaRegistration" in names:
                found.extend(_routes_of(node, source))
                return
        for c in node.named_children:
            walk(c)

    walk(root)
    return found or None


def _routes_of(cls: Node, source: bytes) -> list[Registration]:
    """The ``MapRoute`` templates of one ``AreaRegistration`` class, each with its namespaces
    (the ``namespaces`` argument, else the registration's own namespace and below — MVC's
    default)."""
    default = ((namespace_of(cls, source), True),)
    out: list[Registration] = []
    stack = [cls]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "invocation_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or node_text(fn, source).rsplit(".", 1)[-1] != "MapRoute":
            continue
        args = node.child_by_field_name("arguments")
        positional = [a for a in (args.named_children if args is not None else [])
                      if a.type == "argument"]
        template: str | None = None
        namespaces: tuple[tuple[str, bool], ...] | None = None
        for i, a in enumerate(positional):
            value = a.named_children[-1] if a.named_children else None
            label = a.child_by_field_name("name")
            key = node_text(label, source) if label is not None else None
            if (key == "url" or (key is None and i == 1)) and _string(value, source) is not None:
                template = _string(value, source)
            elif (key == "namespaces" or key is None) and value is not None and \
                    value.type.endswith("array_creation_expression"):
                items = [_string(s, source) for s in _strings_in(value)]
                if items and all(i is not None for i in items):
                    namespaces = tuple((s.removesuffix(".*"), s.endswith(".*"))
                                       for s in items if s is not None)
        if template is not None:
            out.append((namespaces or default, template))
    return out


def _strings_in(node: Node) -> list[Node]:
    out: list[Node] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type.endswith("string_literal"):
            out.append(n)
            continue
        stack.extend(reversed(n.named_children))
    return out


def _resolve_registrations(values: list[list[Registration]], index: Any) -> tuple[Registration, ...] | None:
    return tuple(r for file_values in values for r in file_values) or None


register_fact(FactSpec(name=REGISTRATIONS, language="csharp", gate=(b"AreaRegistration",),
                       collect=_collect_registrations, resolve=_resolve_registrations))


# ---- one controller's area ---------------------------------------------------------------

#: An area that exists but whose value cannot be read: its addresses are unknown.
UNKNOWN = object()


@dataclass(frozen=True)
class Area:
    #: ``[RouteArea]`` prefix for attribute routes; None = none; UNKNOWN = unresolvable.
    route_prefix: object
    #: ``[Area("X")]`` value for the ``[area]`` token; None = none; UNKNOWN = unresolvable.
    token: object
    #: Convention template (``Tests/{controller}/{action}/{id}``); None = none.
    convention: str | None
    #: The controller is in an area but no convention route for it is known.
    convention_unknown: bool


NO_AREA = Area(None, None, None, False)


def _attribute(cls_node: Node, name: str, source: bytes) -> Node | None:
    for attrs in cls_node.named_children:
        if attrs.type != "attribute_list":
            continue
        for a in attrs.named_children:
            nm = a.child_by_field_name("name") or next(
                (c for c in a.named_children if c.type in ("identifier", "qualified_name")), None)
            if a.type == "attribute" and nm is not None and \
                    node_text(nm, source).rsplit(".", 1)[-1].removesuffix("Attribute") == name:
                return a
    return None


def _attr_args(attr: Node, source: bytes) -> tuple[list[Node], dict[str, Node]]:
    """(positional argument values, named argument values) of an attribute."""
    args = next((c for c in attr.named_children if c.type == "attribute_argument_list"), None)
    positional: list[Node] = []
    named: dict[str, Node] = {}
    for a in args.named_children if args is not None else []:
        if a.type != "attribute_argument":
            continue
        kids = a.named_children
        if len(kids) == 2 and kids[0].type == "identifier":
            named[node_text(kids[0], source)] = kids[1]
        elif kids:
            positional.append(kids[-1])
    return positional, named


def controller_area(cls_node: Node | None, source: bytes, path: str, index: Any | None) -> Area:
    """The area a controller's actions are served under (see the module docstring)."""
    if cls_node is None:
        return NO_AREA
    namespace = namespace_of(cls_node, source)
    route_prefix: object = None
    route_area = _attribute(cls_node, "RouteArea", source)
    if route_area is not None:
        positional, named = _attr_args(route_area, source)
        value = named.get("AreaPrefix") or (positional[0] if positional else None)
        route_prefix = string_value(value, source, index, namespace) if value is not None \
            else UNKNOWN
        if route_prefix is None:
            route_prefix = UNKNOWN
    token: object = None
    core_area = _attribute(cls_node, "Area", source)
    if core_area is not None:
        positional, _ = _attr_args(core_area, source)
        token = string_value(positional[0], source, index, namespace) if positional else None
        if token is None:
            token = UNKNOWN
    registrations: tuple[Registration, ...] = index_fact(index, REGISTRATIONS) or ()
    templates = {template for spaces, template in registrations
                 if any(namespace == ns or (below and namespace.startswith(ns + "."))
                        for ns, below in spaces)}
    convention = next(iter(templates)) if len(templates) == 1 else None
    in_area = (route_area is not None or core_area is not None
               or "/areas/" in "/" + path.lower())
    return Area(route_prefix, token, convention, in_area and convention is None)


def convention_endpoint(template: str, controller: str, action: str) -> str | None:
    """``Tests/{controller}/{action}/{id}`` → ``/Tests/<controller>/<action>``: the template up
    to ``{action}`` with the names in. None when the template has no ``{controller}`` /
    ``{action}`` pair (a fixed or unusual shape)."""
    segments = [s for s in template.strip("/").split("/") if s]
    out: list[str] = []
    for seg in segments:
        low = seg.lower()
        if low.startswith("{controller"):
            out.append(controller)
        elif low.startswith("{action"):
            out.append(action)
            break
        elif seg.startswith("{"):
            return None  # a parameter before {action}: not a plain controller/action route
        else:
            out.append(seg)
    else:
        return None
    if controller not in out:
        return None
    return "/" + "/".join(out)
