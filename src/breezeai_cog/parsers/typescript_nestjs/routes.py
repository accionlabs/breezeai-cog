"""Decorator-declared route detection for TS controllers: ``@Controller('base')`` /
``@JsonController('base')`` classes whose methods carry ``@Get(':id')`` / ``@Post()`` etc.
Covers both **NestJS** (``@nestjs/common``) and **routing-controllers** — the decorator
grammar is identical, so detection keys on decorator *names*, not the import source; the
caller passes the resolved ``framework`` label. Emits ``semanticType="route"`` statements
parented to their handler method (via the shared id convention, so parentId matches the
base TypeScript parser's function id).

Also detects **@nestjs/graphql code-first operations** — ``@Query``/``@Mutation``/
``@Subscription`` methods on an ``@Resolver`` class — emitting them as ``framework="graphql"``
routes (``routeKind`` = the operation kind), mirroring the TypeScript resolver-map/SDL
detector so the backend joins them uniformly."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Decorator, Statement
from ...parsers.typescript.decorators import decorator
from ...parsers.typescript.functions import _type_text, extract_params
from ..treesitter import node_text

_METHOD_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Options": "OPTIONS", "Head": "HEAD", "All": "ALL",
}
# @nestjs/microservices message consumers → eventbus_consumer semanticType.
_MESSAGING_DECORATORS = {"EventPattern": "EVENT", "MessagePattern": "MESSAGE"}
# @nestjs/graphql code-first operations. Gated on the class being an @Resolver — @Query is
# also a @nestjs/common *param* decorator, but that lives on a parameter, not in the method's
# decorator list, so a method-level @Query on a resolver is unambiguously the GraphQL one.
# @ResolveField/@ResolveProperty are field resolvers, not client-callable operations, so
# they are NOT emitted as routes (matching the TypeScript resolver-map/SDL detector).
_RESOLVER_DECORATORS = {"Resolver"}
_GRAPHQL_OPS = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
# @ResolveField/@ResolveProperty — field-resolver methods on a @Resolver class.
# Unlike @Query/@Mutation they are not root operations, but they are the actual
# implementation of individual GraphQL fields and must appear in the ontology.
_FIELD_RESOLVERS = {"ResolveField", "ResolveProperty"}
# Extracts the return type from a @ResolveField(() => Brand) / @ResolveField(() => [Brand]) arg.
_RESOLVE_RETURN_RE = re.compile(r"=>\s*\[?\s*([A-Za-z_$][\w$]*)")
# Singleton-only variant (no leading `[`): distinguishes a grouping-object return
# `@Mutation(() => NotificationsMutation)` from an array return `@Query(() => [Brand])`.
# Grouping resolvers always return a singleton shell; entity queries return arrays.
_SINGLETON_RETURN_RE = re.compile(r"=>\s*([A-Za-z_$][\w$]*)")
_NAME_OPT_RE = re.compile(r"""\bname\s*:\s*['"`]([^'"`]+)['"`]""")  # @Query(..., { name: 'x' })
_LEADING_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _graphql_op_name(d: Decorator, mname: str) -> str:
    """Operation name for a code-first GraphQL op. An explicit ``{ name: 'x' }`` option
    wins; else a string first arg (a plain name or an SDL fragment like
    ``'products(...): [Product]'``) yields its leading identifier; else the method name —
    the @nestjs/graphql default."""
    for a in d.args:
        m = _NAME_OPT_RE.search(a)
        if m:
            return m.group(1)
    if d.args:
        first = d.args[0].strip()
        if first[:1] in "'\"`":
            m = _LEADING_ID_RE.match(first.strip("'\"`").strip())
            if m:
                return m.group(0)
    return mname
_RESPONSE_DECORATORS = {"ApiResponse", "ApiOkResponse", "ApiCreatedResponse"}
_TYPE_PROP_RE = re.compile(r"\btype\s*:\s*\[?\s*([A-Za-z_$][\w.$]*)")
# return-type → responseDTO: skip generic wrappers and primitives, take the first
# PascalCase type name (``Promise<OrderDto[]>`` → ``OrderDto``, ``void`` → None).
_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NON_DTO_TYPES = {
    "Promise", "Observable", "Array", "Map", "Set", "Record", "Partial", "Readonly",
    "void", "any", "unknown", "never", "null", "undefined", "string", "number",
    "boolean", "object", "bigint", "symbol", "this", "true", "false",
    # NestJS/GraphQL scalar wrappers used as @ResolveField return types (e.g. () => Number)
    "Number", "String", "Boolean",
}


def _dto_from_type(t: str | None) -> str | None:
    if not t:
        return None
    for tok in _ID_RE.findall(t):
        if tok in _NON_DTO_TYPES or not tok[0].isupper():
            continue
        return tok
    return None


def _return_dto(member: Node, source: bytes) -> str | None:
    """Handler return type → responseDTO (fallback when no ``@ApiResponse``)."""
    return _dto_from_type(_type_text(member.child_by_field_name("return_type"), source))
# `@Controller({ path: 'orders', host: '...' })` — pull the string `path` out of the
# object form (the string form `@Controller('orders')` is handled directly).
_PATH_PROP_RE = re.compile(r"""\bpath\s*:\s*['"`]([^'"`]*)['"`]""")


def _unquote(text: str) -> str:
    return text.strip().strip("'\"`")


def _pattern(d) -> str | None:
    """Address/topic of ``@EventPattern('x')`` / ``@MessagePattern({cmd:'y'})``."""
    if not d.args:
        return None
    raw = d.args[0].strip()
    return _unquote(raw) if raw[:1] in "'\"`" else (raw or None)


def _guards(decs: list[Node], source: bytes) -> list[str]:
    """Guard/auth names: ``@UseGuards(...)`` args (NestJS) and ``@Authorized`` (routing-
    controllers). Presence of any drives ``authRequired``."""
    out: list[str] = []
    for dec in decs:
        d = decorator(dec, source)
        if d.name == "UseGuards":
            out.extend(_unquote(a) for a in d.args)
        elif d.name == "Authorized":  # routing-controllers auth decorator
            out.append("Authorized")
    return out


def _response_dto(decs: list[Node], source: bytes) -> str | None:
    """``@ApiResponse({ type: Dto })`` / ApiOkResponse / ApiCreatedResponse → Dto."""
    for dec in decs:
        d = decorator(dec, source)
        if d.name in _RESPONSE_DECORATORS:
            for arg in d.args:
                m = _TYPE_PROP_RE.search(arg)
                if m:
                    return m.group(1)
    return None


def _request_dto(member: Node, source: bytes) -> str | None:
    """Declared type of the ``@Body``-decorated parameter → requestDTO."""
    for p in extract_params(member.child_by_field_name("parameters"), source):
        if any(d.name == "Body" for d in p.decorators):
            return p.type or None
    return None


def _field_resolver_return_dto(dec: Decorator) -> str | None:
    """Extract the GraphQL return type from ``@ResolveField(() => Brand)`` — the first
    identifier after ``=>`` in the first decorator arg, skipping primitive scalars."""
    if not dec.args:
        return None
    m = _RESOLVE_RETURN_RE.search(dec.args[0])
    if m is None:
        return None
    return _dto_from_type(m.group(1))


def _resolver_grouping_type(decs: list[Node], source: bytes) -> str | None:
    """The explicit type arg of ``@Resolver(() => T)`` → ``T``, else ``None`` for plain
    ``@Resolver()`` / ``@Resolver('field')`` (standard entity resolvers)."""
    for dec in decs:
        d = decorator(dec, source)
        if d.name in _RESOLVER_DECORATORS and d.args:
            m = _RESOLVE_RETURN_RE.search(d.args[0])
            if m:
                return m.group(1)
    return None


def _parent_op_for_grouping(
    body: Node, source: bytes, grouping_type: str
) -> tuple[str, str] | None:
    """Scan the class body for a ``@Query``/``@Mutation`` that returns a **singleton**
    ``grouping_type`` — e.g. ``@Mutation(() => NotificationsMutation)`` (no ``[``).
    Returns ``(op_name, 'MUTATION'|'QUERY')`` or ``None``.

    Distinguishes grouping resolvers (singleton shell, ``@Mutation(() => T)``) from
    entity resolvers (array return, ``@Query(() => [T])``): ``_SINGLETON_RETURN_RE``
    does not match when ``[`` follows ``=>`` because ``[`` is not in ``[A-Za-z_$]``."""
    pending: list[Node] = []
    for member in body.named_children:
        if member.type == "decorator":
            pending.append(member)
            continue
        if member.type != "method_definition":
            pending = []
            continue
        for dec in pending:
            d = decorator(dec, source)
            gql_method = _GRAPHQL_OPS.get(d.name)
            if gql_method is None or not d.args:
                continue
            m = _SINGLETON_RETURN_RE.search(d.args[0])
            if m and m.group(1) == grouping_type:
                mname = node_text(member.child_by_field_name("name"), source)
                return _graphql_op_name(d, mname), gql_method.upper()
        pending = []
    return None


def _args_dto(member: Node, source: bytes) -> str | None:
    """The requestDTO for a ``@ResolveField`` method — the first ``@Args()``-decorated
    parameter whose declared type is a non-primitive class (e.g. ``GetBrandsArgs``).
    Prefers a parameter named ``input``/``data`` (conventional DTO name), then takes the
    first class-typed ``@Args()`` param. Returns ``None`` when all ``@Args`` are scalars."""
    preferred: str | None = None
    first_class: str | None = None
    for p in extract_params(member.child_by_field_name("parameters"), source):
        if not any(d.name == "Args" for d in p.decorators):
            continue
        t = _dto_from_type(p.type)
        if t is None:
            continue
        if first_class is None:
            first_class = t
        if p.name in ("input", "data") and preferred is None:
            preferred = t
    return preferred or first_class


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


_CONTROLLER_DECORATORS = {"Controller", "JsonController"}  # NestJS + routing-controllers


def _controller_base(decorators: list[Node], source: bytes) -> str | None:
    for dec in decorators:
        d = decorator(dec, source)
        if d.name in _CONTROLLER_DECORATORS:
            if not d.args:
                return ""
            arg = d.args[0].strip()
            if arg.startswith("{"):  # object form: @Controller({ path: 'x', host: ... })
                m = _PATH_PROP_RE.search(arg)
                return m.group(1) if m else ""
            return _unquote(arg)
    return None


def _version(decorators: list[Node], source: bytes) -> str | None:
    """``@Version('2')`` (URI versioning) on a method or the controller → version tag."""
    for dec in decorators:
        d = decorator(dec, source)
        if d.name == "Version" and d.args:
            return _unquote(d.args[0])
    return None


def _class_with_decorators(root: Node):
    """Yield (class_declaration, decorator_nodes) for top-level classes."""
    pending: list[Node] = []
    for child in root.named_children:
        if child.type == "decorator":
            pending.append(child)
            continue
        decs, cls = list(pending), None
        pending = []
        if child.type == "export_statement":
            decs += [c for c in child.named_children if c.type == "decorator"]
            cls = next((c for c in child.named_children if c.type == "class_declaration"), None)
        elif child.type == "class_declaration":
            cls = child
        if cls is not None:
            yield cls, decs


def detect_nest_routes(
    root: Node, source: bytes, path: str, *, seen_ids: set[str], framework: str = "nestjs"
) -> list[Statement]:
    routes: list[Statement] = []
    for cls, decs in _class_with_decorators(root):
        base = _controller_base(decs, source)  # None when the class is not a @Controller
        is_controller = base is not None
        is_resolver = any(decorator(dec, source).name in _RESOLVER_DECORATORS for dec in decs)
        class_name = node_text(cls.child_by_field_name("name"), source)
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        ctrl_guards = _guards(decs, source) if is_controller else []
        ctrl_version = _version(decs, source) if is_controller else None
        # Grouping resolver: @Resolver(() => T) where a @Mutation/@Query on the class
        # returns a singleton T — @ResolveField methods inherit the parent op's identity.
        parent_op_info: tuple[str, str] | None = None
        if is_resolver:
            grouping_type = _resolver_grouping_type(decs, source)
            if grouping_type:
                parent_op_info = _parent_op_for_grouping(body, source, grouping_type)
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "comment":
                continue  # a comment between a route decorator and its handler must not drop it
            if member.type == "method_definition":
                mname = node_text(member.child_by_field_name("name"), source)
                mline = member.start_point[0] + 1
                parent = function_id(path, mname, mline, class_name=class_name)
                guards = ctrl_guards + _guards(pending, source)  # merge controller + method
                for dec in pending:
                    d = decorator(dec, source)
                    verb = _METHOD_DECORATORS.get(d.name) if is_controller else None
                    msg = _MESSAGING_DECORATORS.get(d.name)
                    gql = _GRAPHQL_OPS.get(d.name) if is_resolver else None
                    fld = d.name if (is_resolver and d.name in _FIELD_RESOLVERS) else None
                    if verb is None and msg is None and gql is None and fld is None:
                        continue
                    sl, sc = dec.start_point[0] + 1, dec.start_point[1]
                    common = dict(
                        id=disambiguate(statement_id(path, sl, sc), seen_ids),
                        parentId=parent,
                        nodeType="synthetic",
                        text=node_text(dec, source).split("\n", 1)[0],
                        framework=framework,
                        handler=mname,
                        handlerLine=mline,
                        isRegex=False,
                        authRequired=bool(guards),
                        guards=guards or None,
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    )
                    if verb is not None:  # HTTP route
                        routes.append(Statement(
                            semanticType="route",
                            method=verb,
                            endpoint=_join(base, _unquote(d.args[0]) if d.args else ""),
                            routeKind="route",
                            version=_version(pending, source) or ctrl_version,
                            requestDTO=_request_dto(member, source),
                            responseDTO=_response_dto(pending, source) or _return_dto(member, source),
                            **common,
                        ))
                    elif msg is not None:  # @EventPattern / @MessagePattern microservice consumer
                        routes.append(Statement(
                            semanticType="eventbus_consumer",
                            method=msg,
                            endpoint=_pattern(d),
                            routeKind="message",
                            **common,
                        ))
                    elif gql is not None:  # @Query/@Mutation/@Subscription code-first GraphQL op
                        routes.append(Statement(
                            **{**common, "framework": "graphql"},
                            semanticType="route",
                            method=gql.upper(),
                            endpoint=_graphql_op_name(d, mname),
                            routeKind=gql,
                            responseDTO=_field_resolver_return_dto(d) or _return_dto(member, source),
                        ))
                    elif fld is not None:  # @ResolveField/@ResolveProperty field resolver
                        if parent_op_info is not None:
                            # Grouping resolver: inherit parent @Mutation/@Query identity.
                            op_name, op_method = parent_op_info
                            routes.append(Statement(
                                **{**common, "framework": "graphql"},
                                semanticType="route",
                                method=op_method,
                                endpoint=f"{op_name}.{mname}",
                                routeKind=op_method.lower(),
                                requestDTO=_args_dto(member, source),
                                responseDTO=_field_resolver_return_dto(d) or _return_dto(member, source),
                            ))
                        else:
                            routes.append(Statement(
                                **{**common, "framework": "graphql"},
                                semanticType="route",
                                method="QUERY",
                                endpoint=mname,
                                routeKind="field_resolver",
                                requestDTO=_args_dto(member, source),
                                responseDTO=_field_resolver_return_dto(d) or _return_dto(member, source),
                            ))
            pending = []
    return routes
