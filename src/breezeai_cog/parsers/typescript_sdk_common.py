"""Shared helpers for the TypeScript vendor-SDK detectors (``typescript_aws``,
``typescript_hubspot``, ``typescript_chargebee``, ``typescript_tsforce``, ``typescript_apollo``).

An SDK-driven tool (a migration/sync job, an integration worker) has no server framework —
its defining behaviour is *outbound* calls to third-party APIs through a vendor SDK. Those
calls go through a typed client object, not an ``axios``/``fetch`` HTTP client, so the shared
``detection/api_calls.py`` classifier (HTTP-verb + client-hint) never fires on them. Each SDK
detector recovers them with these helpers.

Every SDK detector reuses the existing ``api_call`` semantic type (an outbound call to an
external service — no schema change). The vendor goes on ``framework``
(``hubspot``/``chargebee``/…); the SDK operation goes on ``endpoint``
(``crm.objects.searchApi.doSearch``, ``customer.list``) — the honest identifier of what is
called, since the real HTTP URL/verb live inside the SDK. ``method`` is left **null**
(honest-null: no HTTP verb is visible at the call site).

**Detection is additive** — each SDK detector is registered for TypeScript and layers on top
of whatever parser owns the file (a NestJS service can also call HubSpot). It is import-keyed:
the file must import the SDK (cheap byte guard), the call's receiver chain must root at the
bound client identifier, AND the tail method must be a known SDK operation. All three are
required, so a same-named method on an unrelated object is not mis-tagged (honest — absent
beats wrong).

Every label the SDK detectors emit — ``aws-s3``/``aws-cognito``/``aws-ssm`` and the vendor
values ``hubspot``/``chargebee``/``salesforce`` — is in the Code Ontology Parser Target Spec's
``framework`` enum (§2.4), so a consumer filtering on the documented enum sees these
statements. Adding a new label means updating the spec in the same change. Spec:
https://accionlabs.atlassian.net/wiki/x/BIAGl
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from .emits import Emit
from .treesitter import node_text


@dataclass(frozen=True)
class Sdk:
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


def walk_calls(root: Node) -> list[Node]:
    out: list[Node] = []

    def go(n: Node) -> None:
        if n.type == "call_expression":
            out.append(n)
        for c in n.named_children:
            go(c)

    go(root)
    return out


def callee_chain(call: Node, source: bytes) -> tuple[str, str, str] | None:
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


def annotation_type(node: Node, source: bytes) -> str | None:
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


def client_identifiers(root: Node, source: bytes, sdk: Sdk) -> set[str]:
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
            if annotation_type(n, source) in types:  # return-type annotation
                fname = n.child_by_field_name("name")
                if fname is not None:
                    factories.add(node_text(fname, source))
        for c in n.named_children:
            scan_factories(c)

    scan_factories(root)

    def go(n: Node) -> None:
        if n.type == "required_parameter":  # (client: Chargebee)
            if annotation_type(n, source) in types:
                name = n.child_by_field_name("pattern") or (
                    n.named_children[0] if n.named_children else None
                )
                if name is not None and name.type == "identifier":
                    ids.add(node_text(name, source))
        elif n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is None or name.type != "identifier":
                pass
            elif annotation_type(n, source) in types:  # let c: Client | null
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


def emit_outbound(
    call: Node,
    endpoint: str,
    framework: str,
    out: list[Emit],
    *,
    method: str | None = None,
    reclassify_db: bool = False,
) -> None:
    """An ``api_call`` for ``call``: enriches the enclosing statement in place when it is
    unclassified — or, for SDKs whose calls the generic ORM classifier grabs first (ts-force →
    ``db_method_call``), when ``reclassify_db`` and the statement is that ORM mis-tag; otherwise
    appends (so a genuinely different classified span is never overwritten)."""
    out.append(Emit(
        call,
        {
            "semanticType": "api_call",
            "framework": framework,
            # empty → honest-null (unresolved SObject); never a blank string
            "endpoint": endpoint or None,
            "method": method,  # None for SDK calls (no HTTP verb visible); jQuery passes its verb
            "dataAccessHint": None,  # clear the ORM hint if we reclassified a db_method_call
        },
        enrich="unclassified",
        reclassify=frozenset({"db_method_call"}) if reclassify_db else frozenset(),
    ))


def this_member_call(call: Node, source: bytes) -> tuple[str, str] | None:
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

def detect_client_chain(root: Node, source: bytes, sdk: Sdk, out: list[Emit]) -> bool:
    """Client-chain SDK calls (``client.<resource>.<op>(...)``): the receiver must resolve to
    the SDK's client type and the tail must be one of its operations; endpoint = the call
    chain. Returns True if any call was emitted."""
    clients = client_identifiers(root, source, sdk)
    if not clients:
        return False  # SDK imported but no client bound in this file → nothing to attribute
    emitted = False
    for call in walk_calls(root):
        chain = callee_chain(call, source)
        if chain is None:
            continue
        callee, receiver, tail = chain
        if receiver not in clients or tail not in sdk.operations:
            continue
        emit_outbound(call, callee, sdk.framework, out)
        emitted = True
    return emitted
