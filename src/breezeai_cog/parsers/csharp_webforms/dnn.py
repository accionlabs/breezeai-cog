"""DotNetNuke manifest facts and entry-point detection helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..additive import FactSpec, index_fact, register_fact
from ..dnn_common import parse_dnn_manifest, resolve_dnn_control_source
from ..dotnet_common import simple_attr_name
from ..emits import Emit
from ..treesitter import node_text

FACT = "csharp.dnn-module-controls"
ROUTE_FACT = "csharp.dnn-route-prefixes"
_HTTP_METHODS = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpDelete": "DELETE",
    "HttpPatch": "PATCH",
    "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}


def _manifest_controls(
    source: bytes,
    rel: str,
    repo_root: Path | None,
) -> list[dict[str, str]]:
    try:
        _, controls = parse_dnn_manifest(source)
    except ET.ParseError:
        return []
    out: list[dict[str, str]] = []
    if repo_root is None:
        return out
    for control in controls:
        src = control["source"]
        if not src.lower().endswith(".ascx"):
            continue
        actual = resolve_dnn_control_source(repo_root, rel, src)
        if actual is None:
            continue
        out.append(
            {
                "module": control["module"],
                "controlKey": control["controlKey"],
                "source": actual,
                "manifest": rel,
            }
        )
    return out


def _collect(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None
) -> list[dict[str, str]] | None:
    controls = _manifest_controls(source, rel, repo_root)
    return controls or None


def _resolve(
    values: list[list[dict[str, str]]], index: Any
) -> dict[str, tuple[dict[str, str], ...]] | None:
    by_source: dict[str, set[tuple[tuple[str, str], ...]]] = {}
    for controls in values:
        for control in controls:
            by_source.setdefault(control["source"], set()).add(tuple(sorted(control.items())))
    resolved = {
        source: tuple(dict(item) for item in sorted(entries))
        for source, entries in by_source.items()
    }
    return resolved or None


register_fact(
    FactSpec(
        name=FACT,
        language="csharp",
        gate=(b"<moduleControl", b"<modulecontrol"),
        collect=_collect,
        resolve=_resolve,
        sources=(".dnn",),
    )
)


def _called_name(invocation: Node, source: bytes) -> str | None:
    function = invocation.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "member_access_expression":
        name = function.child_by_field_name("name")
        return node_text(name, source) if name is not None else None
    return node_text(function, source) if function.type == "identifier" else None


def _arguments(invocation: Node, source: bytes) -> list[tuple[str | None, Node]]:
    arguments = invocation.child_by_field_name("arguments")
    if arguments is None:
        return []
    values: list[tuple[str | None, Node]] = []
    for argument in arguments.named_children:
        if argument.type == "argument" and argument.named_children:
            name = argument.child_by_field_name("name")
            value = argument.named_children[-1]
            if name is value:
                value = argument.named_children[0]
            values.append((node_text(name, source) if name is not None else None, value))
        else:
            values.append((None, argument))
    return values


def _string_literal(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type not in ("string_literal", "verbatim_string_literal"):
        return None
    value = node_text(node, source)
    if value.startswith("@"):
        value = value[1:]
    return value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else None


def _string_array(node: Node | None, source: bytes) -> tuple[str, ...] | None:
    if node is None:
        return None
    initializer = (
        node
        if node.type in ("array_initializer", "initializer_expression")
        else next(
            (
                child
                for child in node.named_children
                if child.type in ("array_initializer", "initializer_expression")
            ),
            None,
        )
    )
    if initializer is None:
        return None
    values: list[str] = []
    for child in initializer.named_children:
        value = _string_literal(child, source)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _route_arguments(invocation: Node, source: bytes) -> dict[str, Node]:
    """Bind MapHttpRoute arguments to the documented IMapRoute parameter names."""
    items = _arguments(invocation, source)
    bound = {name.lower(): value for name, value in items if name is not None}
    positional = [value for name, value in items if name is None]
    names = ("moduleFolderName", "routeName", "url")
    for name, value in zip(names, positional[:3], strict=False):
        bound.setdefault(name.lower(), value)
    tail = positional[3:]
    if tail:
        if len(tail) == 1 and _string_array(tail[0], source) is not None:
            bound.setdefault("namespaces", tail[0])
        else:
            bound.setdefault("defaults", tail[0])
            if len(tail) > 1:
                bound.setdefault("constraints", tail[1])
            if len(tail) > 2:
                bound.setdefault("namespaces", tail[2])
    return bound


def _in_register_routes(node: Node, source: bytes) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "method_declaration":
            name = parent.child_by_field_name("name")
            return name is not None and node_text(name, source) == "RegisterRoutes"
        parent = parent.parent
    return False


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _route_registrations(
    root: Node | None,
    source: bytes,
    rel: str,
    repo_root: Path | None,
) -> list[dict[str, str]] | None:
    if root is None or b"IServiceRouteMapper" not in source:
        return None
    found: list[dict[str, str]] = []
    for node in _walk(root):
        if (
            node.type != "invocation_expression"
            or _called_name(node, source) != "MapHttpRoute"
            or not _in_register_routes(node, source)
        ):
            continue
        args = _route_arguments(node, source)
        module = _string_literal(args.get("modulefoldername"), source)
        template = _string_literal(args.get("url"), source)
        namespaces = _string_array(args.get("namespaces"), source) or ()
        if not module or template is None:
            continue
        found.append(
            {
                "module": module.replace("\\", "/").strip("/"),
                "template": template,
                "namespaces": "|".join(namespaces),
                "source": rel,
            }
        )
    return found or None


def _resolve_routes(
    values: list[list[dict[str, str]]],
    index: Any,
) -> tuple[dict[str, str], ...] | None:
    unique = {tuple(sorted(route.items())) for registrations in values for route in registrations}
    return tuple(dict(items) for items in sorted(unique)) or None


register_fact(
    FactSpec(
        name=ROUTE_FACT,
        language="csharp",
        gate=(b"MapHttpRoute",),
        collect=_route_registrations,
        resolve=_resolve_routes,
    )
)


def _attrs(decorators: list[Decorator]) -> set[str]:
    return {simple_attr_name(d.name) for d in decorators}


def _route_for(path: str, class_name: str, index: Any | None) -> dict[str, str] | None:
    parts = path.replace("\\", "/").split("/")
    registrations = index_fact(index, ROUTE_FACT) or ()
    after_desktop = ""
    try:
        desktop = next(i for i, part in enumerate(parts) if part.lower() == "desktopmodules")
        after_desktop = "/".join(parts[desktop + 1 :])
    except StopIteration:
        pass
    types = getattr(index, "types", {}) if index is not None else {}

    def matches_namespace(route: dict[str, str]) -> bool:
        namespaces = set(filter(None, route.get("namespaces", "").split("|")))
        if not namespaces:
            return True
        return any(
            fqn.rsplit(".", 1)[-1] == class_name
            and fqn.rsplit(".", 1)[0] in namespaces
            and path in paths
            for fqn, paths in types.items()
        )

    candidates = [
        route
        for route in registrations
        if (after_desktop == route["module"] or after_desktop.startswith(route["module"] + "/"))
        and matches_namespace(route)
    ]
    if not candidates:
        candidates = [route for route in registrations if matches_namespace(route)]
    unique = {
        (route["module"], route["template"], route.get("namespaces", "")) for route in candidates
    }
    return candidates[0] if len(unique) == 1 else None


def _module_control(record: FileRecord, path: str, index: Any | None) -> dict[str, str] | None:
    controls = index_fact(index, FACT) or {}
    source = path[:-3] if path.lower().endswith(".cs") else path
    entries = controls.get(source, ())
    return entries[0] if len(entries) == 1 else None


def _accept_verbs(decorator: Decorator) -> tuple[str, ...] | None:
    methods: list[str] = []
    for raw in decorator.args:
        value = raw.strip().strip('"').rsplit(".", 1)[-1].upper()
        if value not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            return None
        if value not in methods:
            methods.append(value)
    return tuple(methods) if methods else None


def _methods(fn: Function) -> tuple[str | None, ...]:
    methods: list[str] = []
    dynamic_accept_verbs = False
    for decorator in fn.decorators:
        name = simple_attr_name(decorator.name)
        if name == "AcceptVerbs":
            accepted = _accept_verbs(decorator)
            if accepted is None:
                dynamic_accept_verbs = True
            else:
                methods.extend(value for value in accepted if value not in methods)
        elif name in _HTTP_METHODS and _HTTP_METHODS[name] not in methods:
            methods.append(_HTTP_METHODS[name])
    if methods:
        return tuple(methods)
    if dynamic_accept_verbs:
        return (None,)
    return ("GET",)


def _route_metadata(
    fn: Function,
    cls: Class,
) -> tuple[bool | None, list[str] | None]:
    attrs = _attrs([*cls.decorators, *fn.decorators])
    guards = [
        name
        for name in (
            "DnnAuthorize",
            "DnnModuleAuthorize",
            "AllowAnonymous",
            "ValidateAntiForgeryToken",
        )
        if name in attrs
    ]
    if "AllowAnonymous" in attrs:
        required: bool | None = False
    elif "DnnAuthorize" in attrs or "DnnModuleAuthorize" in attrs:
        required = True
    else:
        required = None
    return required, guards or None


def _controller_routes(
    record: FileRecord,
    root: Node,
    path: str,
    index: Any | None,
) -> list[Emit]:
    out: list[Emit] = []
    for cls in (
        candidate
        for candidate in record.classes
        if (candidate.extends or "").rsplit(".", 1)[-1] == "DnnApiController"
    ):
        route = _route_for(path, cls.name, index)
        if route is None:
            continue
        for fn in record.functions:
            if fn.parentId != cls.id or fn.type != "method" or fn.isStatic:
                continue
            if fn.visibility not in (None, "public") or "NonAction" in _attrs(fn.decorators):
                continue
            required, guards = _route_metadata(fn, cls)
            controller = cls.name.removesuffix("Controller")
            template = route["template"].strip("/")
            template = template.replace("{controller}", controller).replace("{action}", fn.name)
            endpoint = f"/DesktopModules/{route['module']}/API/{template}"
            for method in _methods(fn):
                out.append(
                    Emit(
                        root,
                        dict(
                            semanticType="route",
                            method=method,
                            endpoint=endpoint,
                            framework="dnn",
                            handler=fn.name,
                            handlerLine=fn.startLine,
                            routeKind="route",
                            authRequired=required,
                            guards=guards,
                        ),
                        parent=fn.id,
                        node_type="synthetic",
                        text=f"DNN {cls.name}.{fn.name}",
                        line=fn.startLine,
                        col=0,
                        end_line=fn.endLine,
                    )
                )
    return out


def _control_mount(
    record: FileRecord,
    root: Node,
    path: str,
    index: Any | None,
) -> list[Emit]:
    control = _module_control(record, path, index)
    if control is None:
        return []
    cls = next(
        (
            candidate
            for candidate in record.classes
            if (candidate.extends or "").rsplit(".", 1)[-1] == "PortalModuleBase"
        ),
        None,
    )
    if cls is None:
        return []
    return [
        Emit(
            root,
            dict(
                semanticType="route",
                method="ANY",
                endpoint=f"/DesktopModules/{control['module']}/{control['controlKey']}",
                framework="dnn",
                handler=cls.name,
                handlerLine=cls.startLine,
                routeKind="mount",
            ),
            parent=record.id,
            node_type="synthetic",
            text=f"DNN module control {control['controlKey']}",
            line=cls.startLine,
            col=0,
            end_line=cls.endLine,
        )
    ]


def detect_dnn_routes(
    record: FileRecord,
    root: Node,
    path: str,
    index: Any | None,
) -> list[Emit]:
    return _controller_routes(record, root, path, index) + _control_mount(record, root, path, index)
