"""Additive detector: DotNetNuke (DNN) Web API controllers and module controls.

Fires on any C# file that imports DnnApiController or declares PortalModuleBase —
regardless of which parser owns it. DnnApiController files stay with csharp-aspnet;
this detector layers DNN routes on top via run_additive.

Two facts are registered here:
  csharp.dnn-route-prefixes  — MapHttpRoute registrations collected from .cs files
  csharp.dnn-module-controls — moduleControl entries collected from .dnn manifest files
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..additive import (
    DetectContext,
    Detection,
    Detector,
    FactSpec,
    Gate,
    index_fact,
    register_detector,
    register_fact,
)
from ..dotnet_common import simple_attr_name
from ..emits import Emit
from ..treesitter import node_text
from .dnn_common import parse_dnn_manifest, resolve_dnn_control_source

# ── Fact names ────────────────────────────────────────────────────────────────
FACT = "csharp.dnn-module-controls"
ROUTE_FACT = "csharp.dnn-route-prefixes"

# ── HTTP verb maps ─────────────────────────────────────────────────────────────
_HTTP_ATTR_VERBS: dict[str, str] = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpDelete": "DELETE",
    "HttpPatch": "PATCH",
    "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}
# Web API convention: verb inferred from method-name prefix when no HTTP attribute present.
_NAME_PREFIX_VERBS: dict[str, str] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "head": "HEAD",
    "options": "OPTIONS",
}


# ══════════════════════════════════════════════════════════════════════════════
# Fact: csharp.dnn-module-controls  (collected from .dnn manifest files)
# ══════════════════════════════════════════════════════════════════════════════

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


def _collect_controls(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None
) -> list[dict[str, str]] | None:
    controls = _manifest_controls(source, rel, repo_root)
    return controls or None


def _resolve_controls(
    values: list[list[dict[str, str]]], index: Any
) -> dict[str, tuple[dict[str, str], ...]] | None:
    by_source: dict[str, set[tuple[tuple[str, str], ...]]] = {}
    for controls in values:
        for control in controls:
            by_source.setdefault(control["source"], set()).add(
                tuple(sorted(control.items()))
            )
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
        collect=_collect_controls,
        resolve=_resolve_controls,
        sources=(".dnn",),
    )
)


# ══════════════════════════════════════════════════════════════════════════════
# Fact: csharp.dnn-route-prefixes  (collected from .cs files)
# ══════════════════════════════════════════════════════════════════════════════

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
        module_raw = _string_literal(args.get("modulefoldername"), source)
        template_raw = _string_literal(args.get("url"), source)
        namespaces = _string_array(args.get("namespaces"), source) or ()
        # Variable module prefix or variable template are stored as "" (sentinels for unresolved).
        module = module_raw.replace("\\", "/").strip("/") if module_raw else ""
        template = template_raw if template_raw is not None else ""
        found.append(
            {
                "module": module,
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


# ══════════════════════════════════════════════════════════════════════════════
# Detection helpers
# ══════════════════════════════════════════════════════════════════════════════

def _attrs(decorators: list[Decorator]) -> set[str]:
    return {simple_attr_name(d.name) for d in decorators}


def _route_for(path: str, class_name: str, index: Any | None) -> dict[str, str] | None:
    """Return the route registration that matches this controller file.

    Issue 4: No cross-module fallback. If the controller folder doesn't match a
    registration, return None (leaves endpoint empty).
    Issue 7: A registration with module=="" was recorded from an unresolvable variable;
    it acts as a wildcard match for controllers in the repo (leaves endpoint empty).
    """
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

    def route_matches(route: dict[str, str]) -> bool:
        m = route["module"]
        if not m:
            # Unresolved dynamic prefix matches any controller
            return matches_namespace(route)
        has_ns = bool(route.get("namespaces"))
        if after_desktop:
            # Under DesktopModules/{module}/: must match the owning module folder
            if after_desktop == m or after_desktop.startswith(m + "/"):
                return matches_namespace(route)
            return False
        else:
            # Outside DesktopModules/ (e.g. Services/): matches only via explicit namespace
            return has_ns and matches_namespace(route)

    candidates = [
        route
        for route in registrations
        if route_matches(route)
    ]
    if not candidates:
        return None
    unique = {
        (route["module"], route["template"], route.get("namespaces", "")) for route in candidates
    }
    return candidates[0] if len(unique) == 1 else None


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
    """Return the HTTP verbs for this action.

    Issue 2:
    - [HttpGet] etc. -> attribute verb
    - [AcceptVerbs("GET","POST")] -> those verbs
    - No attribute but name prefix matches (Get..., Post..., ...) -> prefix verb
    - No attribute, no recognised prefix -> (None,) (leave method empty)
    """
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
        elif name in _HTTP_ATTR_VERBS and _HTTP_ATTR_VERBS[name] not in methods:
            methods.append(_HTTP_ATTR_VERBS[name])
    if methods:
        return tuple(methods)
    if dynamic_accept_verbs:
        return (None,)
    lower = fn.name.lower()
    for prefix, verb in _NAME_PREFIX_VERBS.items():
        if lower.startswith(prefix):
            return (verb,)
    return (None,)


def _action_name(fn: Function) -> str:
    """Issue 3: Return [ActionName] value when present, else fn.name."""
    for d in fn.decorators:
        if simple_attr_name(d.name) == "ActionName" and d.args:
            return d.args[0].strip().strip('"')
    return fn.name


def _route_metadata(
    fn: Function,
    cls: Class,
) -> tuple[bool | None, list[str] | None]:
    """Issue 6: authRequired semantics.

    - [DnnAuthorize] -> authRequired=True (framework check, certainly rejects anonymous)
    - [AllowAnonymous] -> authRequired=None (makes no claim per extend-capture §3B)
    - [DnnModuleAuthorize] -> authRequired=None (default View level allows anonymous on public pages)
    Both AllowAnonymous and DnnModuleAuthorize remain in guards.
    """
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
    if "DnnAuthorize" in attrs:
        required: bool | None = True
    else:
        required = None
    return required, guards or None


# ══════════════════════════════════════════════════════════════════════════════
# Emit builders
# ══════════════════════════════════════════════════════════════════════════════

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
        for fn in record.functions:
            if fn.parentId != cls.id or fn.type != "method" or fn.isStatic:
                continue
            if fn.visibility not in (None, "public") or "NonAction" in _attrs(fn.decorators):
                continue
            required, guards = _route_metadata(fn, cls)
            controller = cls.name.removesuffix("Controller")
            action = _action_name(fn)
            if route and route.get("module") and route.get("template"):
                template = route["template"].strip("/")
                template = template.replace("{controller}", controller).replace("{action}", action)
                endpoint: str | None = f"/DesktopModules/{route['module']}/API/{template}"
            else:
                endpoint = None
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


# ══════════════════════════════════════════════════════════════════════════════
# Detector entry point
# ══════════════════════════════════════════════════════════════════════════════

def _detect(dc: DetectContext) -> Detection | None:
    if b"DnnApiController" not in dc.source and b"PortalModuleBase" not in dc.source:
        return None
    emits = _controller_routes(dc.record, dc.root, dc.path, dc.ctx.resolution_index)
    return Detection(emits, label="dnn") if emits else None


register_detector(
    Detector(
        name="dnn",
        gate=Gate(
            "csharp",
            order=75,
            skip_fixtures=True,
        ),
        detect=_detect,
        frameworks=("dnn",),
    )
)
