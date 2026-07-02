"""Spring Boot route detection — works off the parsed ``FileRecord`` (the Java parser
already captured ``@RestController``/``@RequestMapping``/``@GetMapping`` onto
``Class.decorators`` / ``Function.decorators``), so no AST re-walk is needed.

Spring Boot **v2 and v3 share the same web annotations** — only the servlet namespace
changed (``javax.*`` → ``jakarta.*``). So one detector covers both; the version is
inferred from imports (:func:`spring_version`) when present.
"""

from __future__ import annotations

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Function, Statement

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


def _split_paths(raw_value: str) -> list[str]:
    """A mapping's path value → the path(s). Handles the brace-array form
    ``{"/a", "/b"}`` (multiple paths) vs a single quoted path (never comma-split — a
    regex path variable like ``{id:\\d{1,3}}`` legitimately contains a comma)."""
    s = raw_value.strip()
    if s.startswith("{"):  # array literal: {"/a", "/b"}
        parts = [p.strip().strip('"').strip() for p in s.strip("{}").split(",")]
        return [p for p in parts if p] or [""]
    return [s.strip('"')]


def _paths_arg(args: list[str]) -> list[str]:
    for raw in args:
        s = raw.strip()
        if "=" in s and (s.startswith("value") or s.startswith("path")):
            return _split_paths(s.split("=", 1)[1])
        if "=" not in s:
            return _split_paths(s)
    return [""]


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
            return _paths_arg(d.args)[0]  # class-level base — a single prefix
    return ""


def _routes_of(dec: Decorator) -> tuple[str | None, list[str]]:
    """(HTTP verb, path(s)) for a mapping annotation — a mapping may declare several
    paths via the array form ``@GetMapping({"/a", "/b"})``, each its own route."""
    if dec.name in _METHOD_MAPPINGS:
        return _METHOD_MAPPINGS[dec.name], _paths_arg(dec.args)
    if dec.name == "RequestMapping":
        return (_request_method(dec.args) or "GET"), _paths_arg(dec.args)
    return None, []


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _request_dto(fn: Function) -> str | None:
    """Declared type of the ``@RequestBody`` parameter → requestDTO (spec C5)."""
    for p in fn.params:
        if any(d.name == "RequestBody" for d in p.decorators):
            return p.type or None
    return None


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
            verb, subs = _routes_of(dec)
            if verb is None:
                continue
            for sub in subs:  # the array form @GetMapping({"/a","/b"}) yields one route each
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
                    isRegex=False,
                    requestDTO=_request_dto(fn),
                    responseDTO=fn.returnType or None,
                    startLine=fn.startLine,
                    endLine=fn.endLine,
                    path=fn.path,
                ))
    return routes
