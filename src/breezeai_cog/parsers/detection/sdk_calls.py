"""Vendor-SDK **outbound integration** call detection for TypeScript/JavaScript.

An SDK-driven tool (a migration/sync job, an integration worker) has no server framework —
its defining behaviour is *outbound* calls to third-party APIs through a vendor SDK. Those
calls go through a typed client object, not an ``axios``/``fetch`` HTTP client, so the shared
``detection/api_calls.py`` classifier (HTTP-verb + client-hint) never fires on them and the
integration edges are invisible. This detector recovers them.

It reuses the existing ``api_call`` semantic type (an outbound call to an external service —
no schema change), mirroring how ``aws_events.py`` reused ``eventbus_*`` for the AWS SDK. The
vendor goes on ``framework`` (``hubspot``/``chargebee``/…); the SDK operation goes on
``endpoint`` (``crm.objects.searchApi.doSearch``, ``customer.list``) — the honest identifier
of what is called, since the real HTTP URL/verb live inside the SDK. ``method`` is left
**null** (honest-null: no HTTP verb is visible at the call site).

**Detection is additive** — invoked from ``TypeScriptParser.extract``, layering on top of
whatever parser owns the file (a NestJS service can also call HubSpot). It is import-keyed:
the file must import the SDK (cheap byte guard), the call's receiver chain must root at the
bound client identifier, AND the tail method must be a known SDK operation. All three are
required, so a same-named method on an unrelated object is not mis-tagged (honest — absent
beats wrong).

Two call-shape families are handled, each verified against real code:

* **Client-chain SDKs** (``client.<resource>.<op>(...)``) — HubSpot (``@hubspot/api-client``)
  and Chargebee (``chargebee``). The receiver must resolve to the SDK *client type* and the
  tail must be a known operation; endpoint = the call chain. See ``_SDKS`` / ``_client_identifiers``.
* **ts-force (Salesforce)** — a SOQL ORM, NOT a client chain: reads go through
  ``RestObject.query<SObject>(...)`` / ``Entity.retrieve(...)``, writes are instance methods.
  The endpoint is the **SObject type** (from the generic ``<T>`` or an ``extends RestObject``
  receiver — recognised from the code's own inheritance, no hardcoded entity list). Because a
  SOQL query is an outbound call to Salesforce (not local data access), these are reclassified
  from the generic ``db_method_call``/``orm`` tag to ``api_call``. See ``_detect_tsforce``.

**Pending ratification:** the ``framework`` vendor values (``hubspot``/``chargebee``/
``salesforce``) are NOT yet in the Code Ontology Parser Target Spec's ``framework`` enum
(§4.1) — same status as ``nextjs``. The backend may drop the value at ingestion until the spec
enum + allow-list are updated. The parser emits the honest label deliberately; adding the
vendors to the enum is the tracked follow-up with the spec owner. Spec:
https://accionlabs.atlassian.net/wiki/x/BIAGl

The registry (``_SDKS``) is one entry per client-chain SDK, so adding another is a small,
tested addition — never a speculative broad list of unverified call shapes.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Function, Statement
from ..index_common import ClassHeritage
from ..treesitter import first_line, node_text


@dataclass(frozen=True)
class _Sdk:
    """One vendor SDK. ``import_marker`` is the cheap byte guard (the package specifier);
    ``framework`` is the emitted vendor label; ``client_types`` are the SDK client type names
    exported by the package (a variable/parameter of this type IS a client — that is how the
    call receiver is resolved, since clients are bound via factories/params, not just ``new``);
    ``operations`` is the set of terminal method names that count as an outbound call for this
    SDK (verified against real code)."""

    import_marker: bytes
    framework: str
    client_types: frozenset[str]
    operations: frozenset[str]


# Verified against real hubspot-tools source. ``client_types`` are the SDK's client class as
# imported; ``operations`` are the SDK's read/write verbs seen on the client chain — kept to
# distinctive integration operations (not a bare ``get``) so a same-named method on an
# unrelated object cannot match by coincidence.
_SDKS: tuple[_Sdk, ...] = (
    _Sdk(
        import_marker=b"@hubspot/api-client",
        framework="hubspot",
        client_types=frozenset({"Client"}),  # import { Client } from '@hubspot/api-client'
        # client.crm.objects.searchApi.doSearch / basicApi.getPage / batchApi.* / create / update / archive
        operations=frozenset(
            {
                "doSearch",
                "getPage",
                "getById",
                "create",
                "update",
                "archive",
                "createOrUpdate",
                "read",
            }
        ),
    ),
    _Sdk(
        import_marker=b"chargebee",
        framework="chargebee",
        client_types=frozenset({"Chargebee"}),  # import Chargebee from 'chargebee'
        # client.customer.list / subscription.list / item.list / *.retrieve / *.create / *.update
        operations=frozenset({"list", "retrieve", "create", "update", "delete"}),
    ),
)

# --- Salesforce (ts-force) -------------------------------------------------------------
# ts-force has NO client.<resource>.<op>() chain — it is a SOQL ORM. Every read funnels
# through ``RestObject.query<SObject>(SObject, qry)`` (also on generated ``Entity.retrieve``
# static methods), and writes are instance methods (``rec.insert()``/``update``/``delete``).
# The outbound target is the **SObject type** — carried in the call's generic ``<Account>`` or
# as the receiver of ``Account.retrieve(...)`` — which we recognise by the code's own
# ``extends RestObject`` inheritance (no hardcoded entity list). The generic ORM classifier
# grabs ``.query`` first as ``db_method_call``; we reclassify those to ``api_call`` since a
# SOQL query is an outbound call to Salesforce, not local data access.
_TSFORCE_MARKER = b"ts-force"
_TSFORCE_BASE = "RestObject"
_TSFORCE_QUERY_METHODS = {
    "query",
    "queryMore",
    "retrieve",
}  # read surface (SObject in <T>/receiver)
_TSFORCE_WRITE_METHODS = {"insert", "update", "delete"}  # instance writes on a RestObject
# ts-force composite/bulk-DML classes: ``new CompositeCollection().update(records)`` writes a
# whole array in one call. The SObject is the element type of the array argument, not the
# receiver (which is the collection), so these resolve the endpoint from the argument.
_TSFORCE_BULK_WRITERS = {"CompositeCollection"}


def _sdks_in(source: bytes) -> list[_Sdk]:
    """SDKs whose import appears in the file (cheap byte guard, before any AST walk)."""
    return [s for s in _SDKS if s.import_marker in source]


def _walk_calls(root: Node) -> list[Node]:
    out: list[Node] = []

    def go(n: Node) -> None:
        if n.type == "call_expression":
            out.append(n)
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _callee_chain(call: Node, source: bytes) -> tuple[str, str, str] | None:
    """(full_callee, root_identifier, tail_method) for a ``a.b.c.op(...)`` call, else None.
    ``root_identifier`` is the leftmost name in the chain (the client), ``tail_method`` the
    final property (the operation)."""
    fn = call.child_by_field_name("function")
    # `await x.y()` / `(x.y)()` put an await/paren wrapper in the function slot — unwrap it.
    while fn is not None and fn.type in ("await_expression", "parenthesized_expression"):
        fn = fn.named_children[0] if fn.named_children else None
    if fn is None or fn.type != "member_expression":
        return None
    prop = fn.child_by_field_name("property")
    tail = node_text(prop, source) if prop is not None else ""
    # descend the leftmost object to the root identifier of the chain
    node: Node | None = fn.child_by_field_name("object")
    while node is not None and node.type == "member_expression":
        node = node.child_by_field_name("object")
    if node is None or node.type != "identifier":
        return None
    return node_text(fn, source), node_text(node, source), tail


def _annotation_type(node: Node, source: bytes) -> str | None:
    """Base type name of a ``: Type`` / ``: Type | null`` / ``: Type<…>`` annotation among a
    node's children (generics/unions stripped to the leading identifier), else None."""
    ann = next((c for c in node.named_children if c.type == "type_annotation"), None)
    if ann is None:
        return None
    inner = ann.named_children[0] if ann.named_children else None
    if inner is None:
        return None
    if inner.type == "union_type":
        inner = next((c for c in inner.named_children if c.type != "predefined_type"), inner)
    if inner.type == "generic_type":
        name = inner.child_by_field_name("name") or (
            inner.named_children[0] if inner.named_children else None
        )
        return node_text(name, source) if name is not None else None
    return node_text(inner, source) if inner.type in ("type_identifier", "identifier") else None


def _client_identifiers(root: Node, source: bytes, sdk: _Sdk) -> set[str]:
    """Local identifiers that resolve to this SDK's client — matched by the SDK's *client
    type* (how clients are really bound: typed params, typed vars, factory return values),
    not just ``new``. Restricts call matching to genuine client receivers (honest — a
    same-named method on an unrelated object is not tagged).

    Recognizes: a variable/parameter annotated ``: <ClientType>``; a variable assigned from
    ``new <ClientType>(…)``; and a variable assigned from a call to a factory function whose
    return type is ``<ClientType>`` (e.g. ``const c = getClient()`` where
    ``getClient(): Client``)."""
    types = sdk.client_types
    ids: set[str] = set()

    # factory functions whose *return type* is a client type → their names
    factories: set[str] = set()

    def scan_factories(n: Node) -> None:
        if n.type in ("function_declaration", "method_definition"):
            if _annotation_type(n, source) in types:  # return-type annotation
                fname = n.child_by_field_name("name")
                if fname is not None:
                    factories.add(node_text(fname, source))
        for c in n.named_children:
            scan_factories(c)

    scan_factories(root)

    def go(n: Node) -> None:
        if n.type == "required_parameter":  # (client: Chargebee)
            if _annotation_type(n, source) in types:
                name = n.child_by_field_name("pattern") or (
                    n.named_children[0] if n.named_children else None
                )
                if name is not None and name.type == "identifier":
                    ids.add(node_text(name, source))
        elif n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is None or name.type != "identifier":
                pass
            elif _annotation_type(n, source) in types:  # let c: Client | null
                ids.add(node_text(name, source))
            else:
                v = n.child_by_field_name("value")
                while v is not None and v.type in ("await_expression", "parenthesized_expression"):
                    v = v.named_children[0] if v.named_children else None
                if v is not None and v.type == "new_expression":  # new Client()
                    ctor = v.child_by_field_name("constructor")
                    if ctor is not None and node_text(ctor, source) in types:
                        ids.add(node_text(name, source))
                elif v is not None and v.type == "call_expression":  # c = getClient()
                    fn = v.child_by_field_name("function")
                    if (
                        fn is not None
                        and fn.type == "identifier"
                        and node_text(fn, source) in factories
                    ):
                        ids.add(node_text(name, source))
        for c in n.named_children:
            go(c)

    go(root)
    return ids


def _enclosing_statement(line: int, statements: list[Statement]) -> Statement | None:
    best: Statement | None = None
    best_span: int | None = None
    for s in statements:
        if s.startLine <= line <= s.endLine:
            span = s.endLine - s.startLine
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def _owner_function(line: int, functions: list[Function], fallback: str) -> str:
    best_id, best_span = fallback, None
    for f in functions:
        if f.startLine <= line <= f.endLine:
            span = f.endLine - f.startLine
            if best_span is None or span < best_span:
                best_id, best_span = f.id, span
    return best_id


def _emit_outbound(
    call: Node,
    line: int,
    endpoint: str,
    framework: str,
    source: bytes,
    path: str,
    record: FileRecord,
    seen: set[str],
    *,
    reclassify_db: bool = False,
) -> None:
    """Enrich the enclosing statement in place, or append a fresh ``api_call``. Enriches when
    the enclosing statement is unclassified — or, for SDKs whose calls the generic ORM
    classifier grabs first (ts-force → ``db_method_call``), when ``reclassify_db`` and the
    statement is that ORM mis-tag; otherwise appends (so a genuinely different classified
    span is never overwritten)."""
    stmt = _enclosing_statement(line, record.statements)
    enrichable = stmt is not None and (
        stmt.semanticType is None or (reclassify_db and stmt.semanticType == "db_method_call")
    )
    ep = endpoint or None  # empty → honest-null (unresolved SObject); never a blank string
    if enrichable and stmt is not None:
        stmt.semanticType = "api_call"
        stmt.framework = framework
        stmt.endpoint = ep
        stmt.method = None  # SDK call carries no HTTP verb (honest-null)
        stmt.dataAccessHint = None  # clear the ORM hint if we reclassified a db_method_call
    else:
        new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
        seen.add(new_id)
        record.statements.append(
            Statement(
                id=new_id,
                parentId=_owner_function(line, record.functions, file_id(path)),
                nodeType=call.type,
                semanticType="api_call",
                text=first_line(node_text(call, source)),
                endpoint=ep,
                framework=framework,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
        )


_APOLLO_BYTE_GUARDS = (
    b"apollo-angular", b"@apollo/client",
    # Type-annotation guards catch barrel imports where the package name isn't in source
    # (e.g. ``import { Apollo } from '@app/core'`` re-exporting from apollo-angular).
    b": Apollo", b": ApolloBase", b": ApolloClient",
    # ApolloAccessor is an app-level wrapper service that exposes named Apollo clients as
    # getters (e.g. .productCatalogueApollo, .searchApollo) — handled via Pattern B below.
    b": ApolloAccessor",
    # ApiClient is a custom wrapper that delegates to Apollo for typed GraphQL operations.
    b": ApiClient",
    # GraphQLService wraps Apollo in Angular services.
    b": GraphQLService",
)
_APOLLO_CLIENT_TYPES = frozenset({"Apollo", "ApolloBase", "ApolloClient", "ApiClient", "GraphQLService"})
_APOLLO_ACCESSOR_TYPES = frozenset({"ApolloAccessor"})
_APOLLO_METHODS = frozenset({
    "query", "mutate", "watchQuery", "subscribe",
    "typedQuery", "typedMutate",  # custom wrappers over Apollo query/mutate
})


def _apollo_accessor_fields(root: Node, source: bytes) -> set[str]:
    """Field names typed as ApolloAccessor — a wrapper service whose getter properties
    (e.g. ``.productCatalogueApollo``, ``.searchApollo``) each return an ``ApolloBase``
    client. Calls go through ``this.FIELD.namedClient.method(...)`` (Pattern B)."""
    fields: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "required_parameter":
            if _annotation_type(n, source) in _APOLLO_ACCESSOR_TYPES:
                name_node = (
                    n.child_by_field_name("pattern")
                    or n.child_by_field_name("name")
                    or next((c for c in n.named_children if c.type == "identifier"), None)
                )
                if name_node is not None:
                    fields.add(node_text(name_node, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return fields


def _this_deep_call(call: Node, source: bytes) -> tuple[str, str, str] | None:
    """(outer_field, inner_field, method) for ``this.F1.F2.method(...)``, else None.
    Handles the ApolloAccessor pattern: ``this.accessor.namedClient.query(...)``."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    method_prop = fn.child_by_field_name("property")
    method = node_text(method_prop, source) if method_prop is not None else ""
    # obj should be: this.F1.F2
    obj = fn.child_by_field_name("object")
    if obj is None or obj.type != "member_expression":
        return None
    inner_prop = obj.child_by_field_name("property")
    inner = node_text(inner_prop, source) if inner_prop is not None else ""
    # base should be: this.F1
    base = obj.child_by_field_name("object")
    if base is None or base.type != "member_expression":
        return None
    outer_prop = base.child_by_field_name("property")
    this_node = base.child_by_field_name("object")
    if this_node is None or this_node.type != "this":
        return None
    outer = node_text(outer_prop, source) if outer_prop is not None else ""
    return outer, inner, method


def _apollo_injected_fields(root: Node, source: bytes) -> set[str]:
    """Field names on ``this`` that carry an Apollo client, identified by constructor-param
    type annotation. Covers ``private apollo: Apollo`` Angular DI style."""
    fields: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "required_parameter":
            if _annotation_type(n, source) in _APOLLO_CLIENT_TYPES:
                name_node = (
                    n.child_by_field_name("pattern")
                    or n.child_by_field_name("name")
                    or next((c for c in n.named_children if c.type == "identifier"), None)
                )
                if name_node is not None:
                    fields.add(node_text(name_node, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return fields


def _this_member_call(call: Node, source: bytes) -> tuple[str, str] | None:
    """(field, method) when the call is ``this.field.method(...)``, else None.
    Handles the Angular DI pattern where apollo is accessed via ``this.apollo``."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    method_prop = fn.child_by_field_name("property")
    method = node_text(method_prop, source) if method_prop is not None else ""
    obj = fn.child_by_field_name("object")
    if obj is None or obj.type != "member_expression":
        return None
    field_prop = obj.child_by_field_name("property")
    base = obj.child_by_field_name("object")
    if field_prop is None or base is None or base.type != "this":
        return None
    return node_text(field_prop, source), method


def _apollo_endpoint(call: Node, source: bytes) -> str | None:
    """Extract the GQL constant name from ``apollo.query({query: Foo})`` — the first
    ``query:``/``mutation:``/``document:`` identifier in the first argument object."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.named_children if c.type == "object"), None)
    if first is None:
        return None
    for pair in first.named_children:
        if pair.type != "pair":
            continue
        k = pair.child_by_field_name("key")
        v = pair.child_by_field_name("value")
        if k is None or v is None:
            continue
        kname = node_text(k, source).strip("'\"")
        if kname in ("query", "mutation", "document"):
            return node_text(v, source) if v.type in ("identifier", "member_expression") else None
    return None


def _detect_apollo_calls(
    root: Node, source: bytes, path: str, record: FileRecord, seen: set[str]
) -> bool:
    """Detect Apollo GraphQL calls in two patterns:

    * **Pattern A** — direct Apollo DI (``this.apollo.query/mutate(...)``): the field is
      annotated as ``Apollo``/``ApolloBase``/``ApolloClient`` and the call is 2-level.
    * **Pattern B** — ApolloAccessor wrapper (``this.accessor.namedClient.query(...)``):
      the field is annotated as ``ApolloAccessor`` (a service that exposes named Apollo
      clients as getters such as ``.productCatalogueApollo``); the call is 3-level.

    Guards on an Apollo byte-marker and at least one recognisable typed field.
    Returns True if any Apollo call was emitted."""
    if not any(m in source for m in _APOLLO_BYTE_GUARDS):
        return False

    # Pattern A: fields typed as direct Apollo client (2-level this.F.method call)
    direct_fields = _apollo_injected_fields(root, source)
    # Pattern B: fields typed as ApolloAccessor wrapper (3-level this.F1.F2.method call)
    accessor_fields = _apollo_accessor_fields(root, source)

    if not direct_fields and not accessor_fields:
        return False

    emitted = False
    for call in _walk_calls(root):
        # Pattern A: this.directField.method(...)
        if direct_fields:
            result = _this_member_call(call, source)
            if result is not None:
                field, method = result
                if field in direct_fields and method in _APOLLO_METHODS:
                    endpoint = _apollo_endpoint(call, source) or f"{field}.{method}"
                    _emit_outbound(
                        call, call.start_point[0] + 1, endpoint, "graphql", source, path,
                        record, seen,
                    )
                    emitted = True
                    continue

        # Pattern B: this.accessorField.namedClient.method(...)
        if accessor_fields:
            deep = _this_deep_call(call, source)
            if deep is not None:
                outer, inner, method = deep
                if outer in accessor_fields and method in _APOLLO_METHODS:
                    endpoint = _apollo_endpoint(call, source) or f"{inner}.{method}"
                    _emit_outbound(
                        call, call.start_point[0] + 1, endpoint, "graphql", source, path,
                        record, seen,
                    )
                    emitted = True

    return emitted


# --- AWS S3 (command pattern) -------------------------------------------------
# AWS SDK v3 uses ``client.send(new PutObjectCommand({...}))`` — a command-pattern
# call, not a method chain.  The endpoint is the Command class name.
_S3_BYTE_GUARD = b"@aws-sdk/client-s3"
_S3_CLIENT_TYPES = frozenset({"S3Client", "S3"})


def _s3_injected_fields(root: Node, source: bytes) -> set[str]:
    """Field names on ``this`` typed as an S3 client (constructor DI pattern)."""
    fields: set[str] = set()

    def walk(n: Node) -> None:
        if n.type == "required_parameter":
            if _annotation_type(n, source) in _S3_CLIENT_TYPES:
                name_node = (
                    n.child_by_field_name("pattern")
                    or n.child_by_field_name("name")
                    or next((c for c in n.named_children if c.type == "identifier"), None)
                )
                if name_node is not None:
                    fields.add(node_text(name_node, source))
        elif n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                if _annotation_type(n, source) in _S3_CLIENT_TYPES:
                    fields.add(node_text(name, source))
                else:
                    v = n.child_by_field_name("value")
                    while v is not None and v.type in ("await_expression", "parenthesized_expression"):
                        v = v.named_children[0] if v.named_children else None
                    if v is not None and v.type == "new_expression":
                        ctor = v.child_by_field_name("constructor")
                        if ctor is not None and node_text(ctor, source) in _S3_CLIENT_TYPES:
                            fields.add(node_text(name, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return fields


def _command_variables(root: Node, source: bytes) -> dict[str, str]:
    """Map variable name → Command class name for ``const cmd = new PutObjectCommand(…)``."""
    out: dict[str, str] = {}

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            v = n.child_by_field_name("value")
            if name is not None and name.type == "identifier" and v is not None:
                if v.type == "new_expression":
                    ctor = v.child_by_field_name("constructor")
                    if ctor is not None:
                        cn = node_text(ctor, source)
                        if cn.endswith("Command"):
                            out[node_text(name, source)] = cn
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _extract_command_name(
    call: Node, source: bytes, cmd_vars: dict[str, str] | None = None,
) -> str | None:
    """Extract the Command class name from ``client.send(new XxxCommand(…))`` or
    ``client.send(commandVar)`` where ``commandVar = new XxxCommand(…)``."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first_arg = next((c for c in args.named_children if c.type != "comment"), None)
    if first_arg is None:
        return None
    if first_arg.type == "await_expression" and first_arg.named_children:
        first_arg = first_arg.named_children[0]
    if first_arg.type == "new_expression":
        ctor = first_arg.child_by_field_name("constructor")
        if ctor is None:
            return None
        name = node_text(ctor, source)
        return name if name.endswith("Command") else None
    # Variable reference: resolve via pre-scanned command variables
    if first_arg.type == "identifier" and cmd_vars:
        return cmd_vars.get(node_text(first_arg, source))
    return None


def _detect_aws_s3_calls(
    root: Node, source: bytes, path: str, record: FileRecord, seen: set[str]
) -> bool:
    """Detect AWS S3 ``client.send(new XxxCommand(…))`` calls.  Handles both
    ``this.field.send(…)`` (DI pattern) and ``variable.send(…)`` (free variable).
    The endpoint is the Command class name."""
    if _S3_BYTE_GUARD not in source:
        return False
    # DI fields (this.s3) — same pattern as Apollo injected fields
    di_fields = _s3_injected_fields(root, source)
    # Free-variable clients — reuse _client_identifiers
    s3_sdk = _Sdk(
        import_marker=_S3_BYTE_GUARD,
        framework="aws-s3",
        client_types=_S3_CLIENT_TYPES,
        operations=frozenset(),
    )
    free_clients = _client_identifiers(root, source, s3_sdk)
    if not di_fields and not free_clients:
        return False
    cmd_vars = _command_variables(root, source)
    emitted = False
    for call in _walk_calls(root):
        command_name: str | None = None
        # Pattern A: this.field.send(new Cmd(…)) or this.field.send(cmdVar) — DI
        if di_fields:
            result = _this_member_call(call, source)
            if result is not None:
                field, method = result
                if field in di_fields and method == "send":
                    command_name = _extract_command_name(call, source, cmd_vars)
        # Pattern B: variable.send(new Cmd(…)) or variable.send(cmdVar) — free variable
        if command_name is None and free_clients:
            chain = _callee_chain(call, source)
            if chain is not None:
                _callee_text, receiver, tail = chain
                if receiver in free_clients and tail == "send":
                    command_name = _extract_command_name(call, source, cmd_vars)
        if command_name is not None:
            _emit_outbound(
                call, call.start_point[0] + 1, command_name, "aws-s3",
                source, path, record, seen,
            )
            emitted = True
    return emitted


def detect_sdk_calls(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    class_heritage: dict[str, ClassHeritage | None] | None = None,
) -> str | None:
    """Enrich/add vendor-SDK ``api_call`` statements on ``record``. Returns the first vendor
    framework label seen (for the file-level rollup), or ``None``.

    ``class_heritage`` is the repo-wide simple-name → heritage index (from the parser's
    ``build_index`` pre-pass). ts-force needs it because the real read call sites
    (``Account.retrieve(...)``, ``client.query<Account>(...)``) live in *different files* than
    the generated ``class Account extends RestObject`` entity — so the SObject set has to be
    resolved repo-wide, not just from this file's classes. Omitting it (``None``) degrades
    gracefully to file-local resolution."""
    seen = {s.id for s in record.statements}
    file_fw: str | None = None

    for sdk in _sdks_in(source):
        clients = _client_identifiers(root, source, sdk)
        if not clients:
            continue  # SDK imported but no client bound in this file → nothing to attribute
        for call in _walk_calls(root):
            chain = _callee_chain(call, source)
            if chain is None:
                continue
            callee, receiver, tail = chain
            if receiver not in clients or tail not in sdk.operations:
                continue
            _emit_outbound(
                call,
                call.start_point[0] + 1,
                callee,
                sdk.framework,
                source,
                path,
                record,
                seen,
            )
            file_fw = file_fw or sdk.framework

    if _detect_tsforce(root, source, path, record, seen, _repo_entities(class_heritage)):
        file_fw = file_fw or "salesforce"

    if _detect_apollo_calls(root, source, path, record, seen):
        file_fw = file_fw or "graphql"

    if _detect_aws_s3_calls(root, source, path, record, seen):
        file_fw = file_fw or "aws-s3"

    return file_fw


def _restobject_entities(record: FileRecord) -> set[str]:
    """SObject entity names = classes that ``extends RestObject`` (the base parser already
    captured ``Class.extends``). Derived from the code's own inheritance — no hardcoded list."""
    return {c.name for c in record.classes if c.extends == _TSFORCE_BASE}


def _repo_entities(class_heritage: dict[str, ClassHeritage | None] | None) -> set[str]:
    """Repo-wide SObject entity names = every class the heritage index records as directly
    ``extends RestObject``. This is the same ``extends``-based rule as ``_restobject_entities``,
    lifted to the whole repo so a read call site can resolve an entity defined in another file.
    A simple name the index collapsed to ``None`` (declared with conflicting bases in >1 file)
    is skipped — honest: an ambiguous name is not a confirmed entity."""
    if not class_heritage:
        return set()
    return {n for n, ch in class_heritage.items() if ch is not None and ch.extends == _TSFORCE_BASE}


def _entity_typed_vars(root: Node, source: bytes, types: set[str]) -> dict[str, str]:
    """Local identifiers whose type resolves to one of ``types`` → that type name. Recognizes
    a variable assigned ``new T(...)`` and a variable/parameter annotated ``: T``. Used both to
    resolve a write receiver to its SObject (``rec.insert()`` where ``types`` = the entities)
    and to resolve a bulk-writer receiver (``bulk.update()`` where ``types`` =
    :data:`_TSFORCE_BULK_WRITERS`); a receiver not found here is left uncaptured (honest — we do
    not guess the instance's type). Sibling of :func:`_client_identifiers` (which resolves SDK
    *client* types, incl. factory returns)."""
    out: dict[str, str] = {}

    def go(n: Node) -> None:
        if n.type == "required_parameter":  # (rec: Account)
            t = _annotation_type(n, source)
            if t in types:
                name = n.child_by_field_name("pattern") or (
                    n.named_children[0] if n.named_children else None
                )
                if name is not None and name.type == "identifier":
                    out[node_text(name, source)] = t
        elif n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                t = _annotation_type(n, source)
                if t in types:  # let rec: Account
                    out[node_text(name, source)] = t
                else:
                    v = n.child_by_field_name("value")
                    while v is not None and v.type in (
                        "await_expression",
                        "parenthesized_expression",
                    ):
                        v = v.named_children[0] if v.named_children else None
                    if v is not None and v.type == "new_expression":  # const rec = new Account()
                        ctor = v.child_by_field_name("constructor")
                        if ctor is not None and node_text(ctor, source) in types:
                            out[node_text(name, source)] = node_text(ctor, source)
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _array_element_type(node: Node, source: bytes) -> str | None:
    """Element type name of a ``: Type[]`` annotation on ``node`` (``accounts: Account[]`` →
    ``Account``), else None. Only the ``T[]`` array syntax is unwrapped; ``Array<T>`` and other
    shapes return None (honest — not resolved rather than guessed)."""
    ann = next((c for c in node.named_children if c.type == "type_annotation"), None)
    if ann is None:
        return None
    inner = ann.named_children[0] if ann.named_children else None
    if inner is None or inner.type != "array_type":
        return None
    el = next(
        (c for c in inner.named_children if c.type in ("type_identifier", "identifier")), None
    )
    return node_text(el, source) if el is not None else None


def _entity_array_vars(root: Node, source: bytes, entities: set[str]) -> dict[str, str]:
    """Identifiers declared as ``Entity[]`` → that entity name. The endpoint of a bulk write
    (``bulk.update(accounts)``) is the SObject *element type* of the array argument, not the
    receiver, so it is resolved from the argument variable's array annotation. Only a
    locally-declared ``: Entity[]`` resolves; an inferred or unannotated array is skipped."""
    out: dict[str, str] = {}

    def go(n: Node) -> None:
        target: Node | None = None
        if n.type == "required_parameter":  # (accounts: Account[])
            target = n.child_by_field_name("pattern") or (
                n.named_children[0] if n.named_children else None
            )
        elif n.type == "variable_declarator":  # const accounts: Account[] = []
            target = n.child_by_field_name("name")
        if target is not None and target.type == "identifier":
            el = _array_element_type(n, source)
            if el is not None and el in entities:
                out[node_text(target, source)] = el
        for c in n.named_children:
            go(c)

    go(root)
    return out


def _first_arg_identifier(call: Node, source: bytes) -> str | None:
    """Name of the first call argument when it is a plain identifier (``f(accounts)`` →
    ``accounts``), else None — inline array literals / spreads / expressions are not resolved
    (honest: the element type is not a single knowable declared symbol there)."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.named_children if c.type != "comment"), None)
    return node_text(first, source) if first is not None and first.type == "identifier" else None


def _type_arg_name(call: Node, source: bytes) -> str | None:
    """The single type argument of ``fn<T>(...)`` → ``T`` (e.g. ``query<Account>`` → Account)."""
    ta = call.child_by_field_name("type_arguments")
    if ta is None:
        return None
    inner = next(
        (c for c in ta.named_children if c.type in ("type_identifier", "identifier")), None
    )
    return node_text(inner, source) if inner is not None else None


def _detect_tsforce(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    seen: set[str],
    repo_entities: set[str],
) -> bool:
    """ts-force outbound detection. Two read shapes + instance writes, endpoint = SObject:

    * ``RestObject.query<Account>(Account, qry)`` / ``client.query<Contact>(qry)`` → SObject
      from the generic ``<T>`` (fallback: a first-arg identifier that is a known entity).
    * ``Account.retrieve(...)`` — static call whose receiver is a known ``RestObject`` entity.
    * ``rec.insert()/update()/delete()`` — instance write. The receiver variable's type is
      resolved to a known SObject (``rec = new Account()`` or ``rec: Account``); an
      unresolvable receiver (``current.delete()`` where ``current`` is a ``Set``, or a var
      returned from a helper) is left **uncaptured** — absent beats wrong. We never tag a
      write we could not confirm is on an SObject.
    * ``bulk.update(accounts)`` — composite/bulk write where the receiver is a
      :data:`_TSFORCE_BULK_WRITERS` instance; the SObject is the element type of the array
      argument (``accounts: Account[]`` → ``Account``), resolved from the argument variable's
      declared array type. Skipped when the receiver isn't a bulk writer, the argument isn't a
      plain identifier, or its element type isn't a known SObject.

    Byte guard: the file either imports ``ts-force`` **or** references a known SObject in the
    three positions a call site uses it — as a static receiver (``Account.``), a constructed
    instance (``new Account``), or a typed binding (``: Account``) — the real call sites import
    the generated entity, not ``ts-force`` itself. The SObject set is this file's
    ``extends RestObject`` classes **plus** ``repo_entities`` (the same set resolved repo-wide),
    so a call whose entity is defined in another file still resolves. The guard is only a cheap
    pre-filter — every emit still requires the receiver to resolve to a confirmed entity, so a
    stray substring match costs a wasted AST walk, never a wrong tag. SObject is resolved from
    the code (``<T>`` / receiver / receiver-var type), else the call is skipped or ``endpoint``
    stays null — never guessed."""
    entities = _restobject_entities(record) | repo_entities
    if _TSFORCE_MARKER not in source and not any(
        e.encode() + b"." in source
        or b"new " + e.encode() in source
        or b": " + e.encode() in source
        for e in entities
    ):
        return False
    entity_vars = _entity_typed_vars(root, source, entities)
    bulk_writer_vars = set(_entity_typed_vars(root, source, _TSFORCE_BULK_WRITERS))
    array_vars = _entity_array_vars(root, source, entities)
    emitted = False

    for call in _walk_calls(root):
        chain = _callee_chain(call, source)
        if chain is None:
            continue
        _callee, receiver, tail = chain
        endpoint: str | None = None

        if tail in _TSFORCE_QUERY_METHODS:
            # read: SObject lives in the generic <T> or a known-entity receiver. Only accept it
            # as the endpoint when it resolves to a real entity — a bare/unbound type param
            # (``client.query<T>(qry)`` inside a generic helper) leaves endpoint null (honest;
            # the concrete SObject is known only at the caller, reachable via the call graph).
            type_arg = _type_arg_name(call, source)
            if type_arg in entities:
                endpoint = type_arg
            elif receiver in entities:
                endpoint = receiver
            # require SOME ts-force signal: a RestObject-family receiver, or a resolved SObject.
            if endpoint is None and receiver != _TSFORCE_BASE and type_arg is None:
                continue
        elif tail in _TSFORCE_WRITE_METHODS:
            # write: resolve the receiver to an SObject — a static write on the class itself
            # (``Account.delete(id)``, rare) or an instance whose variable type is a known
            # entity. An unresolvable receiver is skipped (we don't guess the instance's type).
            if receiver in entities:
                endpoint = receiver
            elif receiver in entity_vars:
                endpoint = entity_vars[receiver]
            elif receiver in bulk_writer_vars:
                # composite/bulk write: endpoint is the SObject element type of the array
                # argument (``bulk.update(accounts)`` where ``accounts: Account[]``).
                arg = _first_arg_identifier(call, source)
                endpoint = array_vars.get(arg) if arg is not None else None
                if endpoint is None:
                    continue  # unresolvable element type → skip (absent beats wrong)
            else:
                continue
        else:
            continue

        _emit_outbound(
            call,
            call.start_point[0] + 1,
            endpoint or "",
            "salesforce",
            source,
            path,
            record,
            seen,
            reclassify_db=True,
        )
        emitted = True

    return emitted
