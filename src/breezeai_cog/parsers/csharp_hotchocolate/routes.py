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

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...logging import get_logger
from ...schemas import Class, Decorator, FileRecord, Function, Statement
from ..csharp_aspnet.routes import _response_dto, simple_attr_name
from ..treesitter import first_line, node_text
from .mappings import (
    ARG_MARKER_ATTRS,
    AUTHORIZE_ATTR,
    DATALOADER_TYPES,
    ERROR_ATTR_PREFIX,
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
from .descriptor import configure_method, descriptor_target, field_declarations
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


def data_loaders(fn: Function, generated: dict[str, str | None] | None = None) -> list[str] | None:
    """Batching-loader parameter types, in declaration order.

    Two ways a parameter is known to be a loader, both backed by a declaration rather than a name
    pattern: its type *is* a loader type (``IDataLoader<int, Author>``), or its type is one the
    source generator emits for a ``[DataLoader]`` method found elsewhere in the repo
    (``ISessionByIdDataLoader``). The generated interface exists nowhere in the source, so the
    second case needs the repo index — without it the field is empty on real code, since named
    loaders are the norm.
    """
    known = generated or {}
    loaders = [p.type for p in fn.params
               if _base_type(p.type).startswith(DATALOADER_TYPES) or _base_type(p.type) in known]
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
    """Attributes worth keeping on the statement: the request-pipeline ones, so a rewritten wire
    shape stays visible, and the declared error types, which are part of the client's contract."""
    return [d for d in fn.decorators
            if simple_attr_name(d.name) in MIDDLEWARE_ATTRS
            or simple_attr_name(d.name).startswith(ERROR_ATTR_PREFIX)]


def _statement(
    cls: Class, fn: Function, *, kind: str, method: str, endpoint: str, text: str,
    seen: set[str], generated_loaders: dict[str, str | None] | None = None,
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
        dataLoaders=data_loaders(fn, generated_loaders),
        decorators=middleware(fn),
        startLine=fn.startLine,
        endLine=fn.endLine,
        path=fn.path,
    )


def operation_statement(
    cls: Class, fn: Function, kind: str, text: str, seen: set[str],
    generated_loaders: dict[str, str | None] | None = None,
) -> Statement:
    """A client-callable operation on a schema root."""
    return _statement(
        cls, fn, kind=kind, method=kind.upper(),
        endpoint=field_name(fn.name, fn.decorators), text=text, seen=seen,
        generated_loaders=generated_loaders,
    )


def field_resolver_statement(
    cls: Class, fn: Function, target: str, text: str, seen: set[str],
    generated_loaders: dict[str, str | None] | None = None,
) -> Statement:
    """A resolver for one field of a data type. Not client-callable — it runs once per parent
    object when that field is selected — so it carries its own ``routeKind`` and an address that
    names the edge it resolves (``Book.author``). ``method`` has no real meaning here (a field
    resolver has no verb); ``QUERY`` matches ``typescript_nestjs/routes.py:373``."""
    return _statement(
        cls, fn, kind="field_resolver", method="QUERY",
        endpoint=f"{target}.{field_name(fn.name, fn.decorators)}", text=text, seen=seen,
        generated_loaders=generated_loaders,
    )


def subscription_topic(fn: Function) -> str | None:
    """The pub/sub topic a subscription consumes, or None when it cannot be resolved.

    Only a **literal** ``[Topic("x")]`` is resolvable. Everything else is left alone, evidenced
    by ChilliCream's own ConferencePlanner workshop:

    * ``[Topic]`` with no argument defaults to the *member* name, not the GraphQL field name —
      its publisher sends to ``nameof(OnSessionScheduledAsync)``, so recording the field name
      ``onSessionScheduled`` would be a fabricated address.
    * ``[Subscribe(With = nameof(SubscribeToX))]`` moves the topic into that method's body,
      where the workshop builds it by interpolation (``$"OnAttendeeCheckedIn_{sessionId}"``).

    Both cases return None: an event address we cannot read is better absent than invented.
    """
    if SUBSCRIBE_ATTR not in _attr_names(fn.decorators):
        return None
    for dec in fn.decorators:
        if simple_attr_name(dec.name) == TOPIC_ATTR and dec.args:
            return dec.args[0].strip().strip('"') or None
    return None


#: Key of the anchor map: (method name, its declaration's start line).
Anchor = tuple[int, int]


def _attribute_position(method: Node, source: bytes, wanted: str) -> Anchor | None:
    """``(line, col)`` of the ``wanted`` attribute on ``method``'s declaration, else None."""
    for lst in method.named_children:
        if lst.type != "attribute_list":
            continue
        for attr in lst.named_children:
            if attr.type != "attribute":
                continue
            name = attr.child_by_field_name("name")
            if name is None:
                continue
            if simple_attr_name(node_text(name, source).rsplit(".", 1)[-1]) == wanted:
                return attr.start_point[0] + 1, attr.start_point[1]
    return None


def method_nodes(root: Node, source: bytes) -> dict[tuple[str, int], Node]:
    """``(method name, declaration start line)`` → its ``method_declaration`` node.

    One walk shared by the two things that need the tree: the topic anchors below, and the
    fluent-descriptor bodies (whose field declarations are statements, not declarations, so they
    cannot be read off the ``FileRecord``).
    """
    nodes: dict[tuple[str, int], Node] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "method_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                nodes[(node_text(name, source), node.start_point[0] + 1)] = node
        stack.extend(node.named_children)
    return nodes


def topic_anchors(
    nodes: dict[tuple[str, int], Node], source: bytes
) -> dict[tuple[str, int], Anchor]:
    """Map each method to the position of the attribute that declares what it consumes.

    A ``Decorator`` on the record carries no position, but the consumer record is emitted
    *because of* one specific attribute — ``[Topic("x")]``, or ``[Subscribe]`` when the topic
    falls back to the field name — so its span should point there rather than at the whole
    member. This one pass over the tree supplies that position; without it the consumer would
    reuse the route's position and its id would depend on the route existing.
    """
    anchors: dict[tuple[str, int], Anchor] = {}
    for key, node in nodes.items():
        pos = (_attribute_position(node, source, TOPIC_ATTR)
               or _attribute_position(node, source, SUBSCRIBE_ATTR))
        if pos is not None:
            anchors[key] = pos
    return anchors


def topic_consumer_statement(
    fn: Function, topic: str, seen: set[str], anchor: Anchor | None = None
) -> Statement:
    """The server-side half of a subscription: it consumes ``topic`` to push to subscribers.

    A subscription has two addresses — clients name the *field*, the server reads a *topic* — so
    each gets its own record, answering "what can a client subscribe to" and "who consumes this
    topic" respectively. Both hang off the same method (``parentId``), and a route inventory and
    an event inventory each see exactly one of them, so nothing is double-counted.
    """
    line, col = anchor if anchor is not None else (fn.startLine, 0)
    return Statement(
        id=disambiguate(statement_id(fn.path or "", line, col), seen),
        parentId=fn.id,
        nodeType="synthetic",
        semanticType="eventbus_consumer",
        text=f"[{TOPIC_ATTR}] {topic}",
        endpoint=topic,
        framework="graphql",
        handler=fn.name,
        handlerLine=fn.startLine,
        startLine=line,
        endLine=line,
        path=fn.path,
    )


def _operation_records(
    cls: Class, fn: Function, kind: str, text: str, seen: set[str],
    anchors: dict[tuple[str, int], Anchor],
    generated_loaders: dict[str, str | None] | None = None,
) -> list[Statement]:
    """The route for one operation, plus a topic consumer when it is a resolvable subscription."""
    route = operation_statement(cls, fn, kind, text, seen, generated_loaders)
    if kind != "subscription":
        return [route]
    topic = subscription_topic(fn)
    if topic is None:
        return [route]
    anchor = anchors.get((fn.name, fn.startLine))
    return [route, topic_consumer_statement(fn, topic, seen, anchor)]


def extend_target(cls: Class) -> str | None:
    """The type an ``[ExtendObjectType(...)]`` class extends, or None if it carries no such
    attribute.

    Handles the argument forms real code uses: ``typeof(Book)``, ``"Query"``,
    ``OperationTypeNames.Query``, and any of those written as a **named** argument
    (``extendsType: typeof(IMarker)``), which the framework's own test suite does.
    """
    for dec in cls.decorators:
        if simple_attr_name(dec.name) != EXTEND_ATTR or not dec.args:
            continue
        arg = dec.args[0].strip()
        if ":" in arg and not arg.startswith(('"', "typeof(")):
            arg = arg.split(":", 1)[1].strip()  # named argument → keep the value
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
    anchors: dict[tuple[str, int], Anchor],
    generated_loaders: dict[str, str | None] | None = None,
) -> list[Statement]:
    """Routes for one ``[ExtendObjectType(target)]`` class: operations when the target is a
    resolved root, field resolvers when it is a data type whose parent binding is visible, and
    nothing at all when the target cannot be classified."""
    kind = _root_kind_of_name(target, record, heritage)
    if kind is not None:
        return [s for fn in methods
                for s in _operation_records(
                    cls, fn, kind, f"[{EXTEND_ATTR}] {fn.name}", seen, anchors,
                    generated_loaders)]
    resolvers = [fn for fn in methods if resolves_parent(fn, target)]
    if not resolvers:
        get_logger("breezeai_cog.parsers").debug(
            "hotchocolate.extend.unresolved", path=path, target=target, cls=cls.name,
            reason="target is neither a resolved root nor a visible parent binding",
        )
        return []
    return [field_resolver_statement(
                cls, fn, target, f"[{EXTEND_ATTR}({target})] {fn.name}", seen, generated_loaders)
            for fn in resolvers]


def descriptor_field_statement(
    configure: Function, kind: str, name: str, call: Node, source: bytes, seen: set[str]
) -> Statement:
    """One field declared fluently inside ``Configure``. Unlike the attribute-derived routes this
    has a real backing node — the ``d.Field(...)`` call — so it keeps that ``nodeType``, matching
    how ``csharp_graphql`` records its builder calls."""
    line, col = call.start_point[0] + 1, call.start_point[1]
    return Statement(
        id=disambiguate(statement_id(configure.path or "", line, col), seen),
        parentId=configure.id,
        nodeType="invocation_expression",
        semanticType="route",
        text=first_line(node_text(call, source))[:120],
        method=kind.upper(),
        endpoint=name,
        framework="graphql",
        handler=name,
        handlerLine=line,
        routeKind=kind,
        isRegex=False,
        startLine=line,
        endLine=call.end_point[0] + 1,
        path=configure.path,
    )


def _descriptor_routes(
    cls: Class, record: FileRecord, nodes: dict[tuple[str, int], Node], source: bytes,
    heritage: dict[str, object], seen: set[str],
) -> list[Statement]:
    """Fields declared fluently by ``cls``, when the type it describes is a resolved root.

    A non-root descriptor declares that type's *shape*: those fields are not endpoints, and
    emitting them is the over-capture the sibling graphql-dotnet parser measured.
    """
    configure = configure_method(record.functions, cls)
    if configure is None:
        return []
    target = descriptor_target(cls, configure)
    if target is None:
        return []
    kind = _root_kind_of_name(target, record, heritage)
    if kind is None:
        return []
    node = nodes.get((configure.name, configure.startLine))
    body = node.child_by_field_name("body") if node is not None else None
    if body is None:
        return []
    receiver = configure.params[0].name if configure.params else None
    if receiver is None:
        return []
    return [descriptor_field_statement(configure, kind, name, call, source, seen)
            for name, call in field_declarations(body, source, receiver)]


def detect_hotchocolate_routes(
    record: FileRecord, root: Node, source: bytes, seen: set[str], index: Any = None
) -> list[Statement]:
    """Every operation and field resolver declared in this file.

    Two declaration sites: a class carrying a root attribute, and a class extending another type
    with ``[ExtendObjectType]``. ``index`` supplies the repo heritage map so a root attributed in
    another file still resolves.
    """
    heritage: dict[str, object] = getattr(index, "class_heritage", None) or {}
    generated_loaders: dict[str, str | None] = getattr(index, "data_loaders", None) or {}
    nodes = method_nodes(root, source)
    anchors = topic_anchors(nodes, source)
    methods: dict[str, list[Function]] = {}
    for fn in record.functions:
        if is_operation(fn):
            methods.setdefault(fn.parentId, []).append(fn)

    routes: list[Statement] = []
    for cls in record.classes:
        own = methods.get(cls.id, [])
        kind = root_kind(cls)
        if kind is not None and own:
            attr = next(name for name, k in ROOT_ATTRS.items() if k == kind)
            routes.extend(s for fn in own
                          for s in _operation_records(
                              cls, fn, kind, f"[{attr}] {fn.name}", seen, anchors,
                              generated_loaders))
            continue
        target = extend_target(cls)
        if target is not None:
            if own:
                routes.extend(_extension_routes(
                    cls, target, own, record, heritage, record.path, seen, anchors))
            continue
        routes.extend(_descriptor_routes(cls, record, nodes, source, heritage, seen))
    return routes
