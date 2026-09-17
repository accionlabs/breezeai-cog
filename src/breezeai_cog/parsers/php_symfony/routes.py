"""Symfony route detection (#[Route(...)] attributes) off the FileRecord."""

from __future__ import annotations

import re

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..php.dto import _GENERIC_FRAMEWORK_TYPES, extract_use_map, resolve_type_to_fqcn


def _parse_route_attr(args: list[str]) -> tuple[str | None, list[str], str | None]:
    """Parse route path, methods list, and route name from attribute arguments."""
    path = None
    methods: list[str] = []
    name = None

    for arg in args:
        arg_str = arg.strip()
        if arg_str.startswith(("methods:", "methods=")):
            # Parse array of methods: methods: ['GET', 'POST'] or methods: 'GET'
            raw = arg_str.split(":", 1)[-1] if ":" in arg_str else arg_str.split("=", 1)[-1]
            found = re.findall(r"['\"]([A-Z_]+)['\"]", raw, re.IGNORECASE)
            methods.extend(m.upper() for m in found)
        elif arg_str.startswith(("name:", "name=")):
            raw = arg_str.split(":", 1)[-1] if ":" in arg_str else arg_str.split("=", 1)[-1]
            name = raw.strip("'\" ")
        elif not arg_str.startswith(
            ("requirements:", "defaults:", "options:", "schemes:", "host:", "condition:")
        ):
            if path is None:
                # Positional path argument or path: '...'
                raw = (
                    arg_str.split(":", 1)[-1]
                    if ":" in arg_str and not arg_str.startswith("/")
                    else arg_str
                )
                path = raw.strip("'\" ")

    return path, methods, name


def _combine_paths(prefix: str | None, path: str | None) -> str:
    pfx = (prefix or "").rstrip("/")
    sub = (path or "").lstrip("/")
    if not pfx and not sub:
        return "/"
    if not pfx:
        return f"/{sub}" if not sub.startswith("/") else sub
    return f"{pfx}/{sub}"


def _is_route_decorator(name: str) -> bool:
    return name == "Route"


def _function_guards(fn) -> list[str] | None:
    guards: list[str] = []
    for dec in fn.decorators:
        if dec.name == "IsGranted" and dec.args:
            guard = dec.args[0].strip("'\"")
            if guard and guard not in guards:
                guards.append(guard)
    return guards or None


def _symfony_request_dto(fn, use_map: dict[str, str], namespace: str) -> str | None:
    # 1. Parameter with #[MapRequestPayload] attribute
    for p in fn.params:
        if any(
            d.name == "MapRequestPayload" or d.name.endswith("\\MapRequestPayload")
            for d in p.decorators
        ):
            if p.type:
                return resolve_type_to_fqcn(p.type, use_map, namespace)

    # 2. FormRequest-equivalent convention: custom DTO/Request type
    for p in fn.params:
        if not p.type:
            continue
        clean = p.type.lstrip("?\\").strip()
        simple = clean.rsplit("\\", 1)[-1]
        if simple in _GENERIC_FRAMEWORK_TYPES:
            continue
        if (
            simple.endswith(("Request", "DTO", "Dto", "Payload", "Input"))
            or "DTO" in simple
            or "Dto" in simple
        ):
            return resolve_type_to_fqcn(p.type, use_map, namespace)

    return None


def _symfony_response_dto(fn, use_map: dict[str, str], namespace: str) -> str | None:
    if fn.returnType:
        return resolve_type_to_fqcn(fn.returnType, use_map, namespace)
    return None


def detect_symfony_routes(
    record: FileRecord,
    seen_ids: set[str],
    root: Any | None = None,
    source: bytes | None = None,
) -> list[Statement]:
    """Detect Symfony #[Route(...)] attributes on classes and methods (off the record)."""
    routes: list[Statement] = []

    if root is not None and source is not None:
        namespace, use_map = extract_use_map(root, source)
    else:
        use_map = {imp.rsplit("\\", 1)[-1]: imp for imp in record.externalImports}
        namespace = ""
        if record.classes:
            first_cls = record.classes[0].name
            if "\\" in first_cls:
                namespace = first_cls.rsplit("\\", 1)[0]

    # Map class id -> class prefix
    class_prefixes: dict[str, str] = {}
    class_map = {c.id: c for c in record.classes}
    for cls in record.classes:
        for dec in cls.decorators:
            if _is_route_decorator(dec.name):
                cpath, _, _ = _parse_route_attr(dec.args)
                if cpath:
                    class_prefixes[cls.id] = cpath

    for fn in record.functions:
        for dec in fn.decorators:
            if _is_route_decorator(dec.name):
                mpath, methods, rname = _parse_route_attr(dec.args)
                cls_prefix = class_prefixes.get(fn.parentId)
                full_path = _combine_paths(cls_prefix, mpath)
                verbs = methods if methods else ["ANY"]
                guards = _function_guards(fn)
                request_dto = _symfony_request_dto(fn, use_map, namespace)
                response_dto = _symfony_response_dto(fn, use_map, namespace)

                cls_name = class_map[fn.parentId].name if fn.parentId in class_map else ""
                handler = f"{cls_name}@{fn.name}" if cls_name else fn.name

                for verb in verbs:
                    sid = disambiguate(statement_id(record.path, fn.startLine, 0), seen_ids)
                    routes.append(
                        Statement(
                            id=sid,
                            parentId=fn.id,
                            nodeType="synthetic",
                            semanticType="route",
                            routeKind="route",
                            method=verb,
                            endpoint=full_path,
                            handler=handler,
                            name=rname,
                            text=dec.text or f"#[Route('{full_path}')]",
                            startLine=fn.startLine,
                            endLine=fn.endLine,
                            path=record.path,
                            framework="symfony",
                            guards=guards,
                            requestDTO=request_dto,
                            responseDTO=response_dto,
                        )
                    )
        # ``Route`` attributes are fully represented by the emitted route statements.
        # Preserve non-routing PHP attributes on the function.
        fn.decorators = [dec for dec in fn.decorators if not _is_route_decorator(dec.name)]

    return routes
