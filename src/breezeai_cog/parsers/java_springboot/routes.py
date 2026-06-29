"""Spring Boot route detection — works off the parsed ``FileRecord`` (the Java parser
already captured ``@RestController``/``@RequestMapping``/``@GetMapping`` onto
``Class.decorators`` / ``Function.decorators``), so no AST re-walk is needed.

Spring Boot **v2 and v3 share the same web annotations** — only the servlet namespace
changed (``javax.*`` → ``jakarta.*``). So one detector covers both; the version is
inferred from imports (:func:`spring_version`) when present.
"""

from __future__ import annotations

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Statement

_METHOD_MAPPINGS = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
_CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}
_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")


def spring_version(record: FileRecord) -> int | None:
    """2 for Spring Boot v2 (javax.*), 3 for v3 (jakarta.*), else None."""
    for imp in record.externalImports:
        if imp.startswith("jakarta."):
            return 3
        if imp.startswith("javax."):
            return 2
    return None


def _path_arg(args: list[str]) -> str:
    for raw in args:
        s = raw.strip()
        if "=" in s and (s.startswith("value") or s.startswith("path")):
            return s.split("=", 1)[1].strip().strip("{}").strip().strip('"')
        if "=" not in s:
            return s.strip('"')
    return ""


def _request_method(args: list[str]) -> str | None:
    for raw in args:
        if "method" in raw and "=" in raw:
            upper = raw.split("=", 1)[1].upper()
            for verb in _VERBS:
                if verb in upper:
                    return verb
    return None


def _mapping_path(decorators: list[Decorator]) -> str:
    for d in decorators:
        if d.name == "RequestMapping":
            return _path_arg(d.args)
    return ""


def _route_of(dec: Decorator) -> tuple[str | None, str]:
    if dec.name in _METHOD_MAPPINGS:
        return _METHOD_MAPPINGS[dec.name], _path_arg(dec.args)
    if dec.name == "RequestMapping":
        return (_request_method(dec.args) or "GET"), _path_arg(dec.args)
    return None, ""


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_spring_routes(record: FileRecord) -> list[Statement]:
    controllers: dict[str, str] = {}  # class id -> base path
    for cls in record.classes:
        if {d.name for d in cls.decorators} & _CONTROLLER_ANNOTATIONS:
            controllers[cls.id] = _mapping_path(cls.decorators)
    if not controllers:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        base = controllers.get(fn.parentId)
        if base is None:
            continue
        for dec in fn.decorators:
            verb, sub = _route_of(dec)
            if verb is None:
                continue
            routes.append(Statement(
                id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                parentId=fn.id,
                nodeType="annotation",
                semanticType="route",
                text=f"@{dec.name}",
                method=verb,
                endpoint=_join(base, sub),
                framework="spring",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="route",
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            ))
    return routes
