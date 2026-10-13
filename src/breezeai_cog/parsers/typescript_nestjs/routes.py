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
from .patterns import message_pattern
from ..treesitter import node_text

_METHOD_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Options": "OPTIONS", "Head": "HEAD", "All": "ALL",
}
# @nestjs/microservices message consumers → eventbus_consumer semanticType.
_MESSAGING_DECORATORS = {"EventPattern": "EVENT", "MessagePattern": "MESSAGE"}
# @nestjs/schedule jobs → timer: the schedule (cron expression, or interval / delay in ms) is
# the endpoint. Gated on the import: Cron / Interval / Timeout are common names.
_SCHEDULE_DECORATORS = {"Cron": "PERIODIC", "Interval": "PERIODIC", "Timeout": "TIMER"}
# @nestjs/graphql code-first operations. Gated on the class being an @Resolver — @Query is
# also a @nestjs/common *param* decorator, but that lives on a parameter, not in the method's
# decorator list, so a method-level @Query on a resolver is unambiguously the GraphQL one.
# @ResolveField/@ResolveProperty are field resolvers — not client-callable, but real API
# surface, so they ARE emitted, with routeKind="field_resolver" and method="RESOLVE_FIELD"
# to keep them separable from entry points (see _FIELD_RESOLVERS below).
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


def _pattern(dec: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    """Address/topic of ``@EventPattern('x')`` / ``@MessagePattern({cmd:'y'})``: the string, or
    canonical JSON for an object pattern (:func:`.patterns.message_pattern`, shared with the
    ``ClientProxy`` sender so the two match); unresolved → None."""
    call = next((c for c in dec.named_children if c.type == "call_expression"), None)
    args = call.child_by_field_name("arguments") if call is not None else None
    first = next((c for c in args.named_children if c.type != "comment"), None) if args else None
    return message_pattern(first, source, consts) if first is not None else None


def _schedule(dec: Node, name: str, source: bytes, consts: dict[str, str | None]) -> str | None:
    """Schedule of ``@Cron(expr)`` (the first argument) or ``@Interval([name,] ms)`` /
    ``@Timeout([name,] ms)`` (the last): a literal or a constant; anything else (a
    ``CronExpression`` member, a runtime value) → None."""
    call = next((c for c in dec.named_children if c.type == "call_expression"), None)
    args = call.child_by_field_name("arguments") if call is not None else None
    values = [c for c in args.named_children if c.type != "comment"] if args is not None else []
    if not values:
        return None
    arg = values[0] if name == "Cron" else values[-1]
    if arg.type == "object":  # @Cron(expr, { name, timeZone }) never puts options first
        return None
    return message_pattern(arg, source, consts)


def _guards(decs: list[Node], source: bytes, *, interceptors: bool = False) -> list[str]:
    """Guard/auth names: ``@UseGuards(...)`` args (NestJS) and ``@Authorized`` (routing-
    controllers). Presence of any drives ``authRequired``. With ``interceptors``, also
    ``@UseInterceptors(...)`` args in decorator order — they run before the handler too (a guard
    in the contract's sense) but say nothing about auth, so they don't drive ``authRequired``."""
    out: list[str] = []
    for dec in decs:
        d = decorator(dec, source)
        if d.name == "UseGuards" or (interceptors and d.name == "UseInterceptors"):
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


def _resolver_type_arg(decs: list[Node], source: bytes) -> str | None:
    """The explicit type arg of ``@Resolver(() => T)`` → ``T``, else ``None`` for plain
    ``@Resolver()`` / ``@Resolver('field')`` (standard entity resolvers)."""
    for dec in decs:
        d = decorator(dec, source)
        if d.name in _RESOLVER_DECORATORS and d.args:
            m = _RESOLVE_RETURN_RE.search(d.args[0])
            if m:
                return m.group(1)
    return None


def _returns_empty_object(member: Node, source: bytes) -> bool:
    """Whether every ``return`` in a method returns an empty object — ``{}``, ``{} as T``,
    ``new T()`` with no arguments. That is what a stub parent's root operation does: the object
    only exists so its fields can be resolved. A method with no return, or one that returns
    anything else (a service call, ``null``, an entity), is not a stub."""
    body = member.child_by_field_name("body")
    returns: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "return_statement":
            returns.append(n)
        elif n.type in ("function_expression", "arrow_function", "function_declaration"):
            return  # a nested function's returns are not this method's
        for c in n.named_children:
            walk(c)

    if body is not None:
        walk(body)

    def empty(v: Node | None) -> bool:
        while v is not None and v.type in ("as_expression", "parenthesized_expression",
                                           "type_assertion", "satisfies_expression"):
            v = v.named_children[0] if v.named_children else None
        if v is None:
            return False
        if v.type == "object":
            return not v.named_children
        if v.type == "new_expression":
            args = v.child_by_field_name("arguments")
            return args is None or not args.named_children
        return False

    return bool(returns) and all(
        empty(r.named_children[0] if r.named_children else None) for r in returns
    )


def _parent_op_for_grouping(
    body: Node, source: bytes, grouping_type: str
) -> tuple[str, str] | None:
    """Scan the class body for a ``@Query``/``@Mutation`` that returns a **singleton**
    ``grouping_type`` as an **empty object** — e.g. ``@Mutation(() => NotificationsMutation)
    notifications() { return {}; }``. Returns ``(op_name, 'MUTATION'|'QUERY')`` or ``None``.

    Distinguishes grouping resolvers (a stub shell) from entity resolvers: an array return
    (``@Query(() => [T])``) does not match ``_SINGLETON_RETURN_RE`` (``[`` is not in
    ``[A-Za-z_$]``), and a singleton op that returns a real entity (``@Mutation(() => Book)
    addBook(...) { return this.books.create(...); }``) is not a stub — its type's fields are data
    fields, not operations."""
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
            if m and m.group(1) == grouping_type and _returns_empty_object(member, source):
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


def _excluded(local: str, verb: str, exclude: tuple[tuple[str, str | None], ...]) -> bool | None:
    """Whether a route (its path without the global prefix) is in ``setGlobalPrefix``'s exclude
    list. A ``:param`` / ``*`` / ``(.*)`` pattern matches by segment; any other pattern syntax
    cannot be decided → None."""
    target = local.strip("/")
    for pat, method in exclude:
        if method is not None and method != verb:
            continue
        p = pat.strip("/")
        if re.search(r"[^\w\-/:.*()]", p):
            return None
        rx = re.escape(p).replace(r"\(\.\*\)", ".*").replace(r"\*", ".*")
        rx = re.sub(r":\w+", "[^/]+", rx)
        if re.fullmatch(rx, target):
            return True
    return False


def _served(local: str, verb: str, class_name: str, path: str, index: object | None) -> str | None:
    """A NestJS HTTP route's full address: the app's ``setGlobalPrefix`` (unless excluded), then
    its ``RouterModule`` path, then the controller and method paths (``local``). A prefix that is
    set but cannot be resolved — or a controller that cannot be tied to its app when one of the
    repo's apps sets a prefix — makes the address unknown → None (contract §8: never a partial
    address)."""
    ctrl_prefixes = getattr(index, "nest_controller_prefixes", None) or {}
    if class_name in ctrl_prefixes:
        mp = ctrl_prefixes[class_name]
        if mp is None:
            return None
        local = _join(mp, local)
    boots = getattr(index, "nest_global_prefixes", None) or {}
    if not boots:
        return local
    anchors = [d for d in boots if d == "" or path.startswith(d + "/")]
    if not anchors:  # not under any app's bootstrap
        return local if all(b == ("", ()) for b in boots.values()) else None
    prefix, exclude = boots[max(anchors, key=len)]  # the deepest app above this controller
    if prefix == "":
        return local
    if prefix is None or exclude is None:
        return None
    hit = _excluded(local, verb, exclude)
    if hit is None:
        return None
    return local if hit else _join(prefix, local)


def detect_nest_routes(
    root: Node, source: bytes, path: str, *, seen_ids: set[str], framework: str = "nestjs",
    index: object | None = None,
) -> list[Statement]:
    """``index`` is the repo's ``TsAliasIndex``: it carries the NestJS global prefix of each app
    and the ``RouterModule`` path of each registered controller (NestJS only)."""
    index_consts = getattr(index, "const_values", None)
    consts: dict[str, str | None] = index_consts if isinstance(index_consts, dict) else {}
    routes: list[Statement] = []
    schedules = framework == "nestjs" and b"@nestjs/schedule" in source
    for cls, decs in _class_with_decorators(root):
        base = _controller_base(decs, source)  # None when the class is not a @Controller
        is_controller = base is not None
        is_resolver = any(decorator(dec, source).name in _RESOLVER_DECORATORS for dec in decs)
        class_name = node_text(cls.child_by_field_name("name"), source)
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        ctrl_guards = _guards(decs, source, interceptors=True) if is_controller else []
        ctrl_auth = bool(_guards(decs, source)) if is_controller else False
        ctrl_version = _version(decs, source) if is_controller else None
        # Grouping resolver: @Resolver(() => T) where a @Mutation/@Query on the class
        # returns a singleton T — @ResolveField methods inherit the parent op's identity.
        parent_op_info: tuple[str, str] | None = None
        if is_resolver:
            grouping_type = _resolver_type_arg(decs, source)
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
                # merge controller + method; interceptors are guards but not auth
                guards = ctrl_guards + _guards(pending, source, interceptors=True)
                auth_required = ctrl_auth or bool(_guards(pending, source))
                for dec in pending:
                    d = decorator(dec, source)
                    verb = _METHOD_DECORATORS.get(d.name) if is_controller else None
                    msg = _MESSAGING_DECORATORS.get(d.name)
                    gql = _GRAPHQL_OPS.get(d.name) if is_resolver else None
                    fld = d.name if (is_resolver and d.name in _FIELD_RESOLVERS) else None
                    job = _SCHEDULE_DECORATORS.get(d.name) if schedules else None
                    if verb is None and msg is None and gql is None and fld is None and job is None:
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
                        authRequired=auth_required,
                        guards=guards or None,
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    )
                    if verb is not None:  # HTTP route
                        local = _join(base, _unquote(d.args[0]) if d.args else "")
                        routes.append(Statement(
                            semanticType="route",
                            method=verb,
                            endpoint=(
                                _served(local, verb, class_name, path, index)
                                if framework == "nestjs" else local
                            ),
                            routeKind="route",
                            version=_version(pending, source) or ctrl_version,
                            requestDTO=_request_dto(member, source),
                            responseDTO=_response_dto(pending, source) or _return_dto(member, source),
                            **common,
                        ))
                    elif job is not None:  # @Cron / @Interval / @Timeout scheduled job
                        routes.append(Statement(
                            **{**common, "isRegex": None, "authRequired": None, "guards": None},
                            semanticType="timer",
                            method=job,
                            endpoint=_schedule(dec, d.name, source, consts),
                        ))
                    elif msg is not None:  # @EventPattern / @MessagePattern microservice consumer
                        routes.append(Statement(
                            semanticType="eventbus_consumer",
                            method=msg,
                            endpoint=_pattern(dec, source, consts),
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
                            # `Parent.field` when @Resolver(() => T) names the parent type, else
                            # the bare field name: a bare `author` does not say which type it
                            # hangs off, and many types have one. Same form csharp-hotchocolate
                            # emits. RESOLVE_FIELD is the spec's verb for this (not QUERY).
                            parent_type = _resolver_type_arg(decs, source)
                            routes.append(Statement(
                                **{**common, "framework": "graphql"},
                                semanticType="route",
                                method="RESOLVE_FIELD",
                                endpoint=f"{parent_type}.{mname}" if parent_type else mname,
                                routeKind="field_resolver",
                                requestDTO=_args_dto(member, source),
                                responseDTO=_field_resolver_return_dto(d) or _return_dto(member, source),
                            ))
            pending = []
    return routes
