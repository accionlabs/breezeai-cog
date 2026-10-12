"""JAX-RS route detection — works off the parsed ``FileRecord`` (the Java parser
already captured ``@Path``/``@GET``/``@Produces`` onto ``Class.decorators`` /
``Function.decorators``), so no AST re-walk is needed.

JAX-RS (``javax.ws.rs.*`` in Jakarta EE ≤ 8, ``jakarta.ws.rs.*`` in ≥ 9) models a
route as an HTTP-method **marker** annotation (``@GET``/``@POST``/…) on a resource
method, with the path built from the class-level ``@Path`` plus an optional
method-level ``@Path``. Unlike Spring, the verb is the annotation *name*, not an
argument.
"""

from __future__ import annotations

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Statement

# Standard @HttpMethod-meta-annotated markers (javax/jakarta.ws.rs).
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}


def jaxrs_version(record: FileRecord) -> int | None:
    """9 for Jakarta RESTful WS (jakarta.ws.rs), 8 for legacy (javax.ws.rs), else None."""
    for imp in record.externalImports:
        if imp.startswith("jakarta.ws.rs"):
            return 9
        if imp.startswith("javax.ws.rs"):
            return 8
    return None


def _path_of(decorators: list[Decorator]) -> str:
    for d in decorators:
        if d.name == "Path":
            return d.args[0].strip('"') if d.args else ""
    return ""


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_jaxrs_routes(
    record: FileRecord,
    app_path: str | None = "",
    sub_bases: dict[str, str | None] | None = None,
) -> list[Statement]:
    """Resource-method routes of one file. ``app_path`` is the module's ``@ApplicationPath``
    ("" = none, None = present but unresolvable); ``sub_bases`` maps a sub-resource class
    (no class-level ``@Path``) to the base its locator serves it under (None = unresolvable).
    An unresolvable prefix leaves the endpoint empty — the address is unknown."""
    # Any class may hold resource methods; the class @Path (if present) is the base, else the
    # base its sub-resource locator gives it (else none: a bare class keeps its own path).
    subs = sub_bases or {}
    bases: dict[str, str | None] = {}
    for cls in record.classes:
        own = _path_of(cls.decorators)
        has_path = any(d.name == "Path" for d in cls.decorators)
        bases[cls.id] = own if has_path or cls.name not in subs else subs[cls.name]
    if not bases:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        if fn.parentId not in bases:
            continue
        base = bases[fn.parentId]
        sub = _path_of(fn.decorators)
        for dec in fn.decorators:
            if dec.name not in _HTTP_METHODS:
                continue
            routes.append(Statement(
                id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                parentId=fn.id,
                nodeType="synthetic",
                semanticType="route",
                text=f"@{dec.name}",
                method=dec.name,
                endpoint=(_join(app_path, _join(base, sub))
                          if app_path is not None and base is not None else None),
                framework="jaxrs",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="route",
                isRegex=False,
                responseDTO=fn.returnType or None,  # JAX-RS has no @RequestBody → requestDTO stays null
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            ))
    return routes
