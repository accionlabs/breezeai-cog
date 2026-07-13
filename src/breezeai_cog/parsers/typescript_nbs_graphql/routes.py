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
from ..treesitter import node_text
from ..typescript.functions import decorator

_OPS = {"Query": "query", "Mutation": "mutation", "Subscription": "subscription"}
_AUTH_DECORATORS = {"RequireScopes", "RequireAPIScope", "Authorized"}
_LEADING_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")


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
    root: Node, source: bytes, path: str, *, seen_ids: set[str]
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
                        startLine=sl,
                        endLine=dec.end_point[0] + 1,
                        path=path,
                    ))
            pending = []
    return routes
