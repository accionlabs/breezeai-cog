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
from ..java.guards import security_guards

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


def _path_of(
    decorators: list[Decorator], key: tuple[str, int], paths: dict[tuple[str, int], str | None] | None
) -> str | None:
    """The ``@Path`` value: "" when there is none, the literal when it resolves, None when it is
    present but does not resolve (a constant from elsewhere — never a code name). ``paths`` are
    the file's values read from the syntax tree (:func:`declared_paths`); without them the
    captured text is taken as a literal."""
    for d in decorators:
        if d.name == "Path":
            if paths is not None:
                return paths.get(key)
            return d.args[0] if d.args else ""
    return ""


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_jaxrs_routes(
    record: FileRecord,
    app_path: str | None = "",
    sub_bases: dict[str, str | None] | None = None,
    filters: list[tuple[str, frozenset[str]]] | None = None,
    paths: dict[tuple[str, int], str | None] | None = None,
) -> list[Statement]:
    """Resource-method routes of one file. ``app_path`` is the module's ``@ApplicationPath``
    ("" = none, None = present but unresolvable); ``sub_bases`` maps a sub-resource class
    (no class-level ``@Path``) to the base its locator serves it under (None = unresolvable).
    An unresolvable prefix leaves the endpoint empty — the address is unknown. ``filters`` are
    the module's ``@Provider`` request filters with their name bindings: an unbound one guards
    every route, a bound one only routes whose class or method carries the binding (Q9)."""
    # Any class may hold resource methods; the class @Path (if present) is the base, else the
    # base its sub-resource locator gives it (else none: a bare class keeps its own path).
    subs = sub_bases or {}
    bases: dict[str, str | None] = {}
    class_decorators = {cls.id: cls.decorators for cls in record.classes}
    class_names = {cls.id: cls.name for cls in record.classes}
    for cls in record.classes:
        own = _path_of(cls.decorators, (cls.name, cls.startLine), paths)
        has_path = any(d.name == "Path" for d in cls.decorators)
        bases[cls.id] = own if has_path or cls.name not in subs else subs[cls.name]
    if not bases:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    bound = {b for _, binds in filters or [] for b in binds}
    for fn in record.functions:
        if fn.parentId not in bases:
            continue
        base = bases[fn.parentId]
        sub = _path_of(fn.decorators, (class_names[fn.parentId], fn.startLine), paths)
        carried = {d.name for d in class_decorators[fn.parentId]} | {d.name for d in fn.decorators}
        outer = [name for name, binds in filters or [] if not binds or binds & carried]
        # A name-binding annotation (@Secured bound to a filter) selects that filter; it is not a
        # guard itself (Spring Security's @Secured shares the name).
        scoped = [g for g in security_guards(class_decorators[fn.parentId], fn.decorators) or []
                  if g.split("(", 1)[0] not in bound]
        guards = (outer + scoped) or None
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
                          if app_path is not None and base is not None and sub is not None
                          else None),
                framework="jaxrs",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="route",
                isRegex=False,
                guards=guards,
                authRequired=True if scoped else None,
                responseDTO=fn.returnType or None,  # JAX-RS has no @RequestBody → requestDTO stays null
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            ))
    return routes
