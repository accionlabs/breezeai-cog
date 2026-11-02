"""DotNetNuke manifest facts and entry-point detection helpers."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..additive import FactSpec, index_fact, register_fact
from ..dotnet_common import ci_resolve, simple_attr_name, to_repo_path
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
    "AcceptVerbs": "ANY",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _attribute(attrs: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((value for key, value in attrs.items() if _local(key) == wanted), None)


def _child_text(element: ET.Element, name: str) -> str | None:
    return next(
        ((child.text or "").strip() for child in element if _local(child.tag) == name),
        None,
    )


def _manifest_module(root: ET.Element, rel: str) -> str:
    for element in root.iter():
        if _local(element.tag) == "foldername" and element.text:
            value = element.text.strip()
            if value:
                return value
    for element in root.iter():
        if _local(element.tag) == "modulename" and element.text:
            value = element.text.strip()
            if value:
                return value
    return Path(rel).stem


def _manifest_controls(
    source: bytes,
    rel: str,
    repo_root: Path | None,
) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    module = _manifest_module(root, rel)
    out: list[dict[str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "modulecontrol":
            continue
        key = _child_text(element, "controlkey")
        src = _child_text(element, "controlsrc")
        if key is None:
            key = _attribute(element.attrib, "controlkey")
        if src is None:
            src = _attribute(element.attrib, "controlsrc")
        if key is None or not src or repo_root is None:
            continue
        relative = to_repo_path(src, posixpath.dirname(rel), "")
        if relative is None or not relative.lower().endswith(".ascx"):
            continue
        actual = ci_resolve(repo_root, relative)
        if actual is None and src.replace("\\", "/").lstrip("/").lower().startswith(
            "desktopmodules/"
        ):
            actual = ci_resolve(repo_root, src.replace("\\", "/").lstrip("/"))
        if actual is None:
            continue
        out.append(
            {
                "module": module,
                "controlKey": key,
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


def _positional_args(invocation: Node) -> list[Node]:
    arguments = invocation.child_by_field_name("arguments")
    if arguments is None:
        return []
    values: list[Node] = []
    for argument in arguments.named_children:
        if argument.type == "argument" and argument.named_children:
            values.append(argument.named_children[-1])
        else:
            values.append(argument)
    return values


def _string_literal(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type not in ("string_literal", "verbatim_string_literal"):
        return None
    value = node_text(node, source)
    if value.startswith("@"):
        value = value[1:]
    return value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else None


def _string_literals(node: Node, source: bytes) -> Iterator[str]:
    if node.type in ("string_literal", "verbatim_string_literal"):
        value = _string_literal(node, source)
        if value is not None:
            yield value
    for child in node.named_children:
        yield from _string_literals(child, source)


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
    if root is None:
        return None
    found: list[dict[str, str]] = []
    for node in _walk(root):
        if node.type != "invocation_expression" or _called_name(node, source) != "MapHttpRoute":
            continue
        args = _positional_args(node)
        module = _string_literal(args[0] if args else None, source)
        template = _string_literal(args[2] if len(args) > 2 else None, source)
        namespaces = next(
            (
                values
                for argument in args[3:]
                if (values := list(_string_literals(argument, source)))
            ),
            [],
        )
        found.append(
            {
                "module": module.replace("\\", "/").strip("/") if module else "",
                "template": template or "{controller}/{action}",
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
    route_matches = [
        route
        for route in registrations
        if after_desktop == route["module"] or after_desktop.startswith(route["module"] + "/")
    ]
    if route_matches:
        unique = {(route["module"], route["template"]) for route in route_matches}
        return route_matches[0] if len(unique) == 1 else None
    namespace_matches = []
    types = getattr(index, "types", {}) if index is not None else {}
    for route in registrations:
        namespaces = set(filter(None, route.get("namespaces", "").split("|")))
        if not namespaces or not route["module"]:
            continue
        if any(
            fqn.rsplit(".", 1)[-1] == class_name
            and fqn.rsplit(".", 1)[0] in namespaces
            and path in paths
            for fqn, paths in types.items()
        ):
            namespace_matches.append(route)
    if namespace_matches:
        unique = {(route["module"], route["template"]) for route in namespace_matches}
        return namespace_matches[0] if len(unique) == 1 else None
    if any(not route["module"] for route in registrations):
        return None
    if registrations and len({route["module"] for route in registrations}) == 1:
        return None
    if after_desktop:
        module = after_desktop.split("/", 1)[0]
        return {"module": module, "template": "{controller}/{action}"}
    controls = index_fact(index, FACT) or {}
    control_matches = {
        entry["module"]
        for source_entries in controls.values()
        for entry in source_entries
        if path.startswith(source_entries[0]["manifest"].rsplit("/", 1)[0] + "/")
    }
    if not control_matches:
        control_matches = {entry["module"] for entries in controls.values() for entry in entries}
    fallback_module: str | None = next(iter(control_matches)) if len(control_matches) == 1 else None
    return (
        {"module": fallback_module, "template": "{controller}/{action}"}
        if fallback_module
        else None
    )


def _controller_class(record: FileRecord) -> Class | None:
    return next(
        (
            cls
            for cls in record.classes
            if (cls.extends or "").rsplit(".", 1)[-1] == "DnnApiController"
        ),
        None,
    )


def _module_control(record: FileRecord, path: str, index: Any | None) -> dict[str, str] | None:
    controls = index_fact(index, FACT) or {}
    source = path[:-3] if path.lower().endswith(".cs") else path
    entries = controls.get(source, ())
    return entries[0] if len(entries) == 1 else None


def _method(fn: Function) -> str:
    attrs = _attrs(fn.decorators)
    for attr, method in _HTTP_METHODS.items():
        if attr in attrs:
            return method
    return "GET"


def _route_metadata(
    fn: Function,
    cls: Class,
) -> tuple[bool | None, list[str] | None]:
    attrs = _attrs([*cls.decorators, *fn.decorators])
    guards = [
        name
        for name in ("DnnAuthorize", "DnnModuleAuthorize", "ValidateAntiForgeryToken")
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
    cls = _controller_class(record)
    if cls is None:
        return []
    route = _route_for(path, cls.name, index)
    if route is None:
        return []
    out: list[Emit] = []
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
        out.append(
            Emit(
                root,
                dict(
                    semanticType="route",
                    method=_method(fn),
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
