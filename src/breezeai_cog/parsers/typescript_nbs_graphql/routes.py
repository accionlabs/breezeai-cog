"""Route detection for an in-house (bespoke) TypeScript code-first GraphQL framework.

Some codebases roll their own code-first GraphQL layer instead of using
``@nestjs/graphql`` or ``type-graphql``: operation decorators (``@Query``/``@Mutation``/
``@Subscription``) and helpers (``@Arg``/``@Ctx``/``resolversFromService``) are *defined
locally* and imported from a project-relative ``decorators`` module, and resolver classes
are plain DI services (``@Service``) rather than ``@Resolver`` classes. Neither the NestJS
detector (needs ``@Resolver`` + ``@nestjs/``) nor the resolver-map/SDL detector (needs a
``Resolvers`` map or a ``type Query {`` block) sees them, so their operations —
often the product's primary API surface — are invisible.

Signals (grounded on a real repo):
* **operation** — a method carrying ``@Query``/``@Mutation``/``@Subscription``. The
  decorator's optional argument is an SDL fragment (``@Query('products(input: X!): Y!')``)
  whose leading identifier is the operation name; with no arg, the method name is used.
* **not an operation** — ``@FieldResolver`` (a field resolver, like ``@ResolveField``), so
  it is excluded — matching the other GraphQL detectors.
* **auth** — ``@RequireScopes``/``@RequireAPIScope`` (class- or method-level) → guards.

Emits ``semanticType="route"``, ``framework="graphql"``, ``routeKind ∈ {query, mutation,
subscription}`` — uniform with the NestJS / graphql-dotnet / resolver-map detectors.
"""

from __future__ import annotations

import re
from typing import Iterator

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Decorator, Statement
from ..treesitter import node_text, parse_source
from ..typescript.decorators import decorator
from ..typescript.functions import extract_params
# Reuse the SDL DTO helpers — the decorator's SDL fragment is re-parsed with the GraphQL
# grammar (below), so `!`/`[]` wrappers and the argument list are handled by the grammar, not
# by hand, exactly as the resolver-map/SDL detector does for `gql`` templates.
from ..typescript_graphql.routes import _base_type_name, _child, _request_dto

_OPS = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
_AUTH_DECORATORS = {"RequireScopes", "RequireAPIScope", "Authorized"}
_LEADING_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")

#: GraphQL built-in scalars are not DTOs — an operation over ``ID``/``String`` carries no
#: payload type, so those are dropped (honest-null), mirroring the primitive filtering the
#: NestJS/resolver-map detectors apply.
_GQL_SCALARS = frozenset({"ID", "String", "Int", "Float", "Boolean"})
#: TS type wrappers/primitives skipped when reading a DTO off an ``@Arg`` parameter type.
_TS_NON_DTO = frozenset(
    {"string", "number", "boolean", "any", "unknown", "void", "object", "Date",
     "Array", "Promise", "Record", "Partial"}
)


def _op_name(d: Decorator, mname: str) -> str:
    """Operation name: the leading identifier of the SDL-fragment string argument
    (``'products(input: X!): Y!'`` → ``products``), else the method name."""
    if d.args:
        first = d.args[0].strip()
        if first[:1] in "'\"`":
            m = _LEADING_ID_RE.match(first.strip("'\"`").strip())
            if m:
                return m.group(0)
    return mname


def _sdl_fragment(arg: str) -> str | None:
    """The SDL text inside a decorator's string argument (``@Mutation('createOrder(input: X!):
    Order')`` → ``createOrder(input: X!): Order``), or None when the argument is not a string."""
    a = arg.strip()
    return a.strip("'\"`").strip() if a[:1] in "'\"`" else None


def _dto_or_none(name: str | None) -> str | None:
    """A DTO type name, or None for an absent/built-in-scalar type (honest-null)."""
    return name if name and name not in _GQL_SCALARS else None


def _first_field_definition(root: Node) -> Node | None:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "field_definition":
            return n
        stack.extend(n.named_children)
    return None


def _sdl_dtos(fragment: str, timeout_micros: int) -> tuple[str | None, str | None]:
    """(requestDTO, responseDTO) from an operation's SDL fragment. The fragment is wrapped into
    a parseable object type and walked with the GraphQL grammar, reusing the SDL DTO helpers:
    requestDTO is the ``input``/``data`` arg (else the first), responseDTO the return type — each
    reduced to its base name (``[OrderLine!]!`` → ``OrderLine``). Scalars → None."""
    wrapped = b"type Query {\n" + fragment.encode("utf-8") + b"\n}"
    try:
        root = parse_source("graphql", wrapped, timeout_micros).root_node
    except Exception:  # a malformed fragment must never abort capture — honest-null instead
        return None, None
    field = _first_field_definition(root)
    if field is None:
        return None, None
    req = _dto_or_none(_request_dto(field, wrapped))
    res = _dto_or_none(_base_type_name(_child(field, "type"), wrapped))
    return req, res


def _ts_base_type(t: str | None) -> str | None:
    """The base DTO class name of a TS type string — the first PascalCase identifier that is not
    a wrapper/primitive (``OrderLine[]`` → ``OrderLine``; ``string`` → None)."""
    if not t:
        return None
    for tok in _LEADING_ID_RE.findall(t):
        if tok[0].isupper() and tok not in _TS_NON_DTO:
            return str(tok)
    return None


def _arg_request_dto(member: Node, source: bytes) -> str | None:
    """Fallback requestDTO from the method's ``@Arg('input'|'data')`` parameter (else the first
    ``@Arg``) as its base class type — used when the SDL fragment declares no input type."""
    preferred: str | None = None
    first: str | None = None
    for p in extract_params(member.child_by_field_name("parameters"), source):
        arg = next((d for d in p.decorators if d.name == "Arg"), None)
        if arg is None:
            continue
        t = _ts_base_type(p.type)
        if t is None:
            continue
        if first is None:
            first = t
        argname = arg.args[0].strip("'\"` ") if arg.args else ""
        if argname in ("input", "data") and preferred is None:
            preferred = t
    return preferred or first


def _guards(decs: list[Node], source: bytes) -> list[str]:
    out: list[str] = []
    for dec in decs:
        d = decorator(dec, source)
        if d.name in _AUTH_DECORATORS:
            out.append(d.name)
    return out


def _classes_with_decorators(root: Node) -> Iterator[tuple[Node, list[Node]]]:
    """Yield (class_declaration, leading_decorator_nodes) for top-level classes,
    including those wrapped in an ``export_statement``."""
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


def detect_nbs_graphql_routes(
    root: Node, source: bytes, path: str, *, seen_ids: set[str], timeout_micros: int = 0
) -> list[Statement]:
    routes: list[Statement] = []
    for cls, cls_decs in _classes_with_decorators(root):
        name_node = cls.child_by_field_name("name")
        body = cls.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        class_name = node_text(name_node, source)
        cls_guards = _guards(cls_decs, source)
        pending: list[Node] = []
        for member in body.named_children:
            if member.type == "decorator":
                pending.append(member)
                continue
            if member.type == "comment":
                continue  # a comment between a decorator and its handler must not drop it
            if member.type == "method_definition":
                mname_node = member.child_by_field_name("name")
                if mname_node is None:
                    pending = []
                    continue
                mname = node_text(mname_node, source)
                mline = member.start_point[0] + 1
                parent = function_id(path, mname, mline, class_name=class_name)
                guards = cls_guards + _guards(pending, source)
                for dec in pending:
                    d = decorator(dec, source)
                    kind = _OPS.get(d.name)
                    if kind is None:
                        continue  # @FieldResolver, @Arg, etc. are not operations
                    sl, sc = dec.start_point[0] + 1, dec.start_point[1]
                    # DTOs: the decorator's SDL fragment is the contract (source of truth); the
                    # method's `@Arg` types are the fallback when it declares no input type.
                    req_dto = res_dto = None
                    frag = _sdl_fragment(d.args[0]) if d.args else None
                    if frag is not None:
                        req_dto, res_dto = _sdl_dtos(frag, timeout_micros)
                    request_dto = req_dto or _arg_request_dto(member, source)
                    routes.append(Statement(
                        id=disambiguate(statement_id(path, sl, sc), seen_ids),
                        parentId=parent,
                        nodeType="synthetic",  # decorator-derived route → no backing AST node
                        semanticType="route",
                        text=node_text(dec, source).split("\n", 1)[0][:200],
                        method=kind.upper(),
                        endpoint=_op_name(d, mname),
                        framework="graphql",
                        handler=mname,
                        handlerLine=mline,
                        routeKind=kind,
                        isRegex=False,
                        authRequired=bool(guards) or None,
                        guards=guards or None,
                        requestDTO=request_dto,
                        responseDTO=res_dto,
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    ))
            pending = []
    return routes
