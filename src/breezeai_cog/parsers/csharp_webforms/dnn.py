"""DotNetNuke manifest facts and entry-point detection helpers."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..additive import FactSpec, index_fact, register_fact
from ..dotnet_common import ci_resolve, simple_attr_name, to_repo_path
from ..emits import Emit

FACT = "csharp.dnn-module-controls"
_DNN_MARKERS = (
    b"DnnApiController",
    b"IServiceRouteMapper",
    b"PortalModuleBase",
    b"DnnAuthorize",
)
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


def _manifest_module(root: ET.Element, rel: str) -> str:
    for element in root.iter():
        if _local(element.tag) in {"foldername", "modulename", "name"} and element.text:
            value = element.text.strip()
            if value:
                return value
    return Path(rel).stem


def _manifest_controls(
    source: bytes, rel: str, repo_root: Path | None,
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
        key = _attribute(element.attrib, "controlkey")
        src = _attribute(element.attrib, "controlsrc")
        if not key or not src or repo_root is None:
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
        out.append({
            "module": module,
            "controlKey": key,
            "source": actual,
            "manifest": rel,
        })
    return out


def _collect(root: Node | None, source: bytes, rel: str, repo_root: Path | None) -> list[dict[str, str]] | None:
    controls = _manifest_controls(source, rel, repo_root)
    return controls or None


def _resolve(values: list[list[dict[str, str]]], index: Any) -> dict[str, tuple[dict[str, str], ...]] | None:
    by_source: dict[str, set[tuple[tuple[str, str], ...]]] = {}
    for controls in values:
        for control in controls:
            by_source.setdefault(control["source"], set()).add(tuple(sorted(control.items())))
    resolved = {
        source: tuple(dict(item) for item in sorted(entries))
        for source, entries in by_source.items()
    }
    return resolved or None


register_fact(FactSpec(
    name=FACT,
    language="csharp",
    gate=(b"<moduleControl", b"<modulecontrol"),
    collect=_collect,
    resolve=_resolve,
    sources=(".dnn",),
))


def _attrs(decorators: list[Decorator]) -> set[str]:
    return {simple_attr_name(d.name) for d in decorators}


def _module_for(path: str, index: Any | None) -> str | None:
    parts = path.replace("\\", "/").split("/")
    try:
        desktop = next(i for i, part in enumerate(parts) if part.lower() == "desktopmodules")
        if desktop + 1 < len(parts):
            return parts[desktop + 1]
    except StopIteration:
        pass
    controls = index_fact(index, FACT) or {}
    matches = {
        entry["module"]
        for source_entries in controls.values()
        for entry in source_entries
        if path.startswith(source_entries[0]["manifest"].rsplit("/", 1)[0] + "/")
    }
    if not matches:
        matches = {entry["module"] for entries in controls.values() for entry in entries}
    return next(iter(matches)) if len(matches) == 1 else None


def _controller_class(record: FileRecord) -> Class | None:
    return next(
        (
            cls for cls in record.classes
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
    fn: Function, cls: Class,
) -> tuple[bool | None, list[str] | None]:
    attrs = _attrs([*cls.decorators, *fn.decorators])
    guards = [name for name in ("DnnAuthorize", "ValidateAntiForgeryToken") if name in attrs]
    if "AllowAnonymous" in attrs:
        required: bool | None = False
    elif "DnnAuthorize" in attrs:
        required = True
    else:
        required = None
    return required, guards or None


def _controller_routes(
    record: FileRecord, root: Node, path: str, index: Any | None,
) -> list[Emit]:
    cls = _controller_class(record)
    module = _module_for(path, index)
    if cls is None or module is None:
        return []
    out: list[Emit] = []
    for fn in record.functions:
        if fn.parentId != cls.id or fn.type != "method" or fn.isStatic:
            continue
        if fn.visibility not in (None, "public") or "NonAction" in _attrs(fn.decorators):
            continue
        required, guards = _route_metadata(fn, cls)
        out.append(Emit(
            root,
            dict(
                semanticType="route",
                method=_method(fn),
                endpoint=f"/DesktopModules/{module}/API/{cls.name.removesuffix('Controller')}/{fn.name}",
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
        ))
    return out


def _control_mount(
    record: FileRecord, root: Node, path: str, index: Any | None,
) -> list[Emit]:
    control = _module_control(record, path, index)
    if control is None:
        return []
    cls = next(
        (
            candidate for candidate in record.classes
            if (candidate.extends or "").rsplit(".", 1)[-1] == "PortalModuleBase"
        ),
        None,
    )
    if cls is None:
        return []
    return [Emit(
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
    )]


def detect_dnn_routes(
    record: FileRecord, root: Node, path: str, index: Any | None,
) -> list[Emit]:
    return _controller_routes(record, root, path, index) + _control_mount(record, root, path, index)
