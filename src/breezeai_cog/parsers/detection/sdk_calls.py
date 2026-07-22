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
_TSFORCE_QUERY_METHODS = {"query", "retrieve"}  # read surface (SObject in <T>/receiver)
_TSFORCE_WRITE_METHODS = {"insert", "update", "delete"}  # instance writes on a RestObject


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


def detect_sdk_calls(root: Node, source: bytes, path: str, record: FileRecord) -> str | None:
    """Enrich/add vendor-SDK ``api_call`` statements on ``record``. Returns the first vendor
    framework label seen (for the file-level rollup), or ``None``."""
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

    if _detect_tsforce(root, source, path, record, seen):
        file_fw = file_fw or "salesforce"

    return file_fw


def _restobject_entities(record: FileRecord) -> set[str]:
    """SObject entity names = classes that ``extends RestObject`` (the base parser already
    captured ``Class.extends``). Derived from the code's own inheritance — no hardcoded list."""
    return {c.name for c in record.classes if c.extends == _TSFORCE_BASE}


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
    root: Node, source: bytes, path: str, record: FileRecord, seen: set[str]
) -> bool:
    """ts-force outbound detection. Two read shapes + instance writes, endpoint = SObject:

    * ``RestObject.query<Account>(Account, qry)`` / ``client.query<Contact>(qry)`` → SObject
      from the generic ``<T>`` (fallback: a first-arg identifier that is a known entity).
    * ``Account.retrieve(...)`` — static call whose receiver is a known ``RestObject`` entity.
    * ``rec.insert()/update()/delete()`` — instance write; endpoint left null unless the
      receiver itself is an entity name (honest-null — we don't guess the instance's type).

    Only fires in a file importing ``ts-force`` (byte guard). SObject is resolved from the
    code (``<T>`` / receiver / known entity), else ``endpoint`` stays null — never guessed."""
    if _TSFORCE_MARKER not in source:
        return False
    entities = _restobject_entities(record)
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
        elif tail in _TSFORCE_WRITE_METHODS and receiver in entities:
            endpoint = receiver
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
