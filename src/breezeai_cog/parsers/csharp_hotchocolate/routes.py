"""HotChocolate operation detection.

HotChocolate declares its API surface with attributes: a class marked ``[QueryType]`` /
``[MutationType]`` / ``[SubscriptionType]`` is a schema root, and each **public** method on it
is one client-callable field. There is no backing AST node for the route itself (it is derived
from the attribute), so emitted statements carry ``nodeType="synthetic"`` — the same shape the
ASP.NET attribute routes and the TypeScript code-first GraphQL detector use.

``framework="graphql"`` (not ``"hotchocolate"``) so the backend joins GraphQL operations
uniformly across stacks, matching ``csharp_graphql`` and both TypeScript GraphQL detectors.

Works off the ``FileRecord`` the base C# parser already produced — its classes carry their
attributes, and its functions carry parameters, parameter attributes and return types — so no
second AST walk is needed for the attribute-declared style.
"""

from __future__ import annotations

from typing import Any

from ...emit import disambiguate, statement_id
from ...logging import get_logger
from ...schemas import Class, Decorator, FileRecord, Function, Statement
from ..csharp_aspnet.routes import _response_dto, simple_attr_name
from .mappings import (
    ARG_MARKER_ATTRS,
    AUTHORIZE_ATTR,
    DATALOADER_TYPES,
    EXTEND_ATTR,
    IGNORE_ATTR,
    INFRA_PARAM_ATTRS,
    INFRA_PARAM_TYPES,
    MIDDLEWARE_ATTRS,
    PARENT_ATTR,
    ROOT_ATTRS,
    SUBSCRIBE_ATTR,
    ROOT_SCHEMA_NAMES,
    TOPIC_ATTR,
)
from .naming import field_name


def _attr_names(decorators: list[Decorator]) -> set[str]:
    return {simple_attr_name(d.name) for d in decorators}


def root_kind(cls: Class) -> str | None:
    """Operation kind if ``cls`` carries a root-declaring attribute, else None."""
    for name in _attr_names(cls.decorators):
        kind = ROOT_ATTRS.get(name)
        if kind is not None:
            return kind
    return None


def is_operation(fn: Function) -> bool:
    """Whether ``fn`` is exposed as a field. HotChocolate exposes public methods only; C#
    members default to ``internal``, so anything not explicitly public stays out."""
    if fn.type != "method" or fn.visibility != "public":
        return False
    return IGNORE_ATTR not in _attr_names(fn.decorators)


def _base_type(type_name: str | None) -> str:
    """``IDataLoader<int, Author>`` → ``IDataLoader``; ``Book`` → ``Book``."""
    if not type_name:
        return ""
    return type_name.split("<", 1)[0].strip().rsplit(".", 1)[-1]


def data_loaders(fn: Function) -> list[str] | None:
    """Declared batching-loader parameter types, in declaration order."""
    loaders = [p.type for p in fn.params if _base_type(p.type).startswith(DATALOADER_TYPES)]
    return loaders or None


def request_dto(fn: Function) -> str | None:
    """The client-argument DTO, or None.

    Only a parameter carrying an argument-marking attribute counts. ``[Service]`` has been
    optional since v13, so an unattributed ``CatalogDb db`` is indistinguishable from a real
    argument without the DI registrations — recording it would put a database context in a
    field meant to hold a request type. Null is the honest answer.
    """
    for p in fn.params:
        if _attr_names(p.decorators) & ARG_MARKER_ATTRS:
            return p.type or None
    return None


def is_infrastructure_param(p_type: str | None, decorators: list[Decorator]) -> bool:
    """Whether a parameter is framework-injected rather than a client argument."""
    if _attr_names(decorators) & INFRA_PARAM_ATTRS:
        return True
    base = _base_type(p_type)
    return base in INFRA_PARAM_TYPES or base.startswith(DATALOADER_TYPES)


def guards_of(cls: Class, fn: Function) -> list[str]:
    """``[Authorize]`` guards inherited from the class plus the method's own, deduped."""
    out: list[str] = []
    for decorators in (cls.decorators, fn.decorators):
        for dec in decorators:
            if simple_attr_name(dec.name) != AUTHORIZE_ATTR:
                continue
            label = f"{AUTHORIZE_ATTR}({', '.join(dec.args)})" if dec.args else AUTHORIZE_ATTR
            if label not in out:
                out.append(label)
    return out


def middleware(fn: Function) -> list[Decorator]:
    """Request-pipeline attributes, kept so a rewritten wire shape stays visible."""
    return [d for d in fn.decorators if simple_attr_name(d.name) in MIDDLEWARE_ATTRS]


def _statement(
    cls: Class, fn: Function, *, kind: str, method: str, endpoint: str, text: str,
    seen: set[str],
) -> Statement:
    """One route record for a resolver method. ``endpoint`` is the field address the framework
    serves; ``handler`` keeps the C# method name."""
    guards = guards_of(cls, fn)
    return Statement(
        id=disambiguate(statement_id(fn.path or "", fn.startLine, 0), seen),
        parentId=fn.id,
        nodeType="synthetic",
        semanticType="route",
        text=text,
        method=method,
        endpoint=endpoint,
        framework="graphql",
        handler=fn.name,
        handlerLine=fn.startLine,
        routeKind=kind,
        isRegex=False,
        authRequired=bool(guards) or None,
        guards=guards or None,
        requestDTO=request_dto(fn),
        responseDTO=_response_dto(fn.returnType),
        dataLoaders=data_loaders(fn),
        decorators=middleware(fn),
        startLine=fn.startLine,
        endLine=fn.endLine,
        path=fn.path,
    )


def operation_statement(
    cls: Class, fn: Function, kind: str, text: str, seen: set[str]
) -> Statement:
    """A client-callable operation on a schema root."""
    return _statement(
        cls, fn, kind=kind, method=kind.upper(),
        endpoint=field_name(fn.name, fn.decorators), text=text, seen=seen,
    )


def field_resolver_statement(
    cls: Class, fn: Function, target: str, text: str, seen: set[str]
) -> Statement:
    """A resolver for one field of a data type. Not client-callable — it runs once per parent
    object when that field is selected — so it carries its own ``routeKind`` and an address that
    names the edge it resolves (``Book.author``). ``method`` has no real meaning here (a field
    resolver has no verb); ``QUERY`` matches ``typescript_nestjs/routes.py:373``."""
    return _statement(
        cls, fn, kind="field_resolver", method="QUERY",
        endpoint=f"{target}.{field_name(fn.name, fn.decorators)}", text=text, seen=seen,
    )


def subscription_topic(fn: Function, endpoint: str) -> str | None:
    """The pub/sub topic a subscription consumes, or None when it cannot be resolved.

    Only the declarative form is resolvable from the declaration: ``[Subscribe]`` plus an
    optional ``[Topic("x")]``, whose default when bare (or absent) is the field name — that
    default is HotChocolate's own, so it is a fact rather than a convention. An attribute
    argument must be a compile-time constant, so an interpolated topic can only appear in a
    ``receiver.SubscribeAsync($"...")`` call inside the body; those carry no ``[Subscribe]`` and
    return None here, because an event address we cannot read is better absent than fabricated.
    """
    if SUBSCRIBE_ATTR not in _attr_names(fn.decorators):
        return None
    for dec in fn.decorators:
        if simple_attr_name(dec.name) == TOPIC_ATTR:
            topic = dec.args[0].strip().strip('"') if dec.args else ""
            return topic or endpoint
    return endpoint


def topic_consumer_statement(fn: Function, topic: str, seen: set[str]) -> Statement:
    """The server-side half of a subscription: it consumes ``topic`` to push to subscribers.

    A subscription has two addresses — clients name the *field*, the server reads a *topic* — so
    each gets its own record, answering "what can a client subscribe to" and "who consumes this
    topic" respectively. Both hang off the same method (``parentId``), and a route inventory and
    an event inventory each see exactly one of them, so nothing is double-counted.
    """
    return Statement(
        id=disambiguate(statement_id(fn.path or "", fn.startLine, 0), seen),
        parentId=fn.id,
        nodeType="synthetic",
        semanticType="eventbus_consumer",
        text=f"[{TOPIC_ATTR}] {topic}",
        endpoint=topic,
        framework="graphql",
        handler=fn.name,
        handlerLine=fn.startLine,
        startLine=fn.startLine,
        endLine=fn.endLine,
        path=fn.path,
    )


def _operation_records(
    cls: Class, fn: Function, kind: str, text: str, seen: set[str]
) -> list[Statement]:
    """The route for one operation, plus a topic consumer when it is a resolvable subscription."""
    route = operation_statement(cls, fn, kind, text, seen)
    if kind != "subscription":
        return [route]
    topic = subscription_topic(fn, route.endpoint or fn.name)
    if topic is None:
        return [route]
    return [route, topic_consumer_statement(fn, topic, seen)]


def extend_target(cls: Class) -> str | None:
    """The type an ``[ExtendObjectType(...)]`` class extends, or None if it carries no such
    attribute. Handles all three argument forms — ``typeof(Book)``, ``"Query"`` and
    ``OperationTypeNames.Query`` — returning the bare type name."""
    for dec in cls.decorators:
        if simple_attr_name(dec.name) != EXTEND_ATTR or not dec.args:
            continue
        arg = dec.args[0].strip()
        if arg.startswith("typeof(") and arg.endswith(")"):
            arg = arg[len("typeof("): -1]
        return arg.strip().strip('"').rsplit(".", 1)[-1] or None
    return None


def _root_kind_of_name(name: str, record: FileRecord, heritage: dict[str, object]) -> str | None:
    """Operation kind for a *named* type, from enforced signals only.

    The schema name (``"Query"`` / ``OperationTypeNames.Query``) names the root directly. A CLR
    class name only counts when that class is seen carrying a root attribute — in this file, or
    via the repo heritage index. A class named ``Query`` that is registered in ``Program.cs`` and
    attributed nowhere stays unresolved: root-ness is declared at the registration site, which
    this parser does not read.
    """
    kind = ROOT_SCHEMA_NAMES.get(name)
    if kind is not None:
        return kind
    for cls in record.classes:
        if cls.name == name and (kind := root_kind(cls)) is not None:
            return kind
    entry = heritage.get(name)  # None means "declared by >1 class, ambiguous" — honest-null
    decorators = getattr(entry, "decorators", None) if entry is not None else None
    for attr in _attr_names(decorators or []):
        if attr in ROOT_ATTRS:
            return ROOT_ATTRS[attr]
    return None


def resolves_parent(fn: Function, target: str) -> bool:
    """Whether ``fn`` binds the object being resolved — the enforced signals being an explicit
    ``[Parent]`` parameter, or a first parameter typed as the extended type (which the resolver
    compiler binds as the parent)."""
    if any(PARENT_ATTR in _attr_names(p.decorators) for p in fn.params):
        return True
    first = fn.params[0] if fn.params else None
    return first is not None and _base_type(first.type) == target


def _extension_routes(
    cls: Class, target: str, methods: list[Function], record: FileRecord,
    heritage: dict[str, object], path: str, seen: set[str],
) -> list[Statement]:
    """Routes for one ``[ExtendObjectType(target)]`` class: operations when the target is a
    resolved root, field resolvers when it is a data type whose parent binding is visible, and
    nothing at all when the target cannot be classified."""
    kind = _root_kind_of_name(target, record, heritage)
    if kind is not None:
        return [s for fn in methods
                for s in _operation_records(cls, fn, kind, f"[{EXTEND_ATTR}] {fn.name}", seen)]
    resolvers = [fn for fn in methods if resolves_parent(fn, target)]
    if not resolvers:
        get_logger("breezeai_cog.parsers").debug(
            "hotchocolate.extend.unresolved", path=path, target=target, cls=cls.name,
            reason="target is neither a resolved root nor a visible parent binding",
        )
        return []
    return [field_resolver_statement(cls, fn, target, f"[{EXTEND_ATTR}({target})] {fn.name}", seen)
            for fn in resolvers]


def detect_hotchocolate_routes(
    record: FileRecord, seen: set[str], index: Any = None
) -> list[Statement]:
    """Every operation and field resolver declared in this file.

    Two declaration sites: a class carrying a root attribute, and a class extending another type
    with ``[ExtendObjectType]``. ``index`` supplies the repo heritage map so a root attributed in
    another file still resolves.
    """
    heritage: dict[str, object] = getattr(index, "class_heritage", None) or {}
    methods: dict[str, list[Function]] = {}
    for fn in record.functions:
        if is_operation(fn):
            methods.setdefault(fn.parentId, []).append(fn)
    if not methods:
        return []

    routes: list[Statement] = []
    for cls in record.classes:
        own = methods.get(cls.id)
        if not own:
            continue
        kind = root_kind(cls)
        if kind is not None:
            attr = next(name for name, k in ROOT_ATTRS.items() if k == kind)
            routes.extend(s for fn in own
                          for s in _operation_records(cls, fn, kind, f"[{attr}] {fn.name}", seen))
            continue
        target = extend_target(cls)
        if target is not None:
            routes.extend(_extension_routes(
                cls, target, own, record, heritage, record.path, seen))
    return routes
