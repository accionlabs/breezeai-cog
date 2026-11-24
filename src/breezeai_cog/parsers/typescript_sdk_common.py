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

from collections.abc import Callable
from dataclasses import dataclass

from tree_sitter import Node

from ..emit import statement_id
from ..schemas import FileRecord
from .detection import match_api
from .emits import Emit, Enrich
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
    enrich: Enrich = "unclassified",
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
        enrich=enrich,
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


# ── The HTTP client of a generic outbound call ──────────────────────────────────────────────
def label_http_client(
    root: Node, source: bytes, path: str, record: FileRecord, framework: str,
    evidenced: Callable[[Node], bool],
) -> list[Emit]:
    """``framework`` on the ``api_call`` statements this file already holds, when the client of
    their HTTP call is certain (``evidenced``: an ``axios`` imported here, the global ``fetch``,
    an injected Angular ``HttpClient``). Never a new statement and never a guess: a statement is
    labelled only when it has no ``framework`` yet and **every** HTTP call in it is ``evidenced``
    (``Promise.all([axios.get(a), fetch(b)])`` gets none)."""
    unlabelled = {s.id: s for s in record.statements
                  if s.semanticType == "api_call" and not s.framework}
    if not unlabelled:
        return []
    from .typescript.statements import collect_http_client_ids
    client_ids = collect_http_client_ids(root, source)
    out: list[Emit] = []
    done: set[str] = set()
    for call in walk_calls(root):
        if not evidenced(call):
            continue
        node: Node | None = call
        while node is not None:
            sid = statement_id(path, node.start_point[0] + 1, node.start_point[1])
            stmt = unlabelled.get(sid)
            if stmt is not None and stmt.nodeType == node.type:
                break
            node = node.parent
        if node is None or sid in done:
            continue
        done.add(sid)
        if all(evidenced(c) for c in walk_calls(node) if _is_http_call(c, source, client_ids)):
            out.append(Emit(node, {"framework": framework}, enrich="same"))
    return out


def _is_http_call(call: Node, source: bytes, client_ids: frozenset[str]) -> bool:
    """Whether the shared outbound classifier reads ``call`` as an HTTP call."""
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    method = callee.rsplit(".", 1)[-1]
    return match_api(callee, method, client_ids, "typescript") is not None


def callee_root(call: Node, source: bytes) -> str | None:
    """The name a call is made through: ``axios`` in ``axios(cfg)`` / ``axios.get(u)``, ``api`` in
    ``api.post(u)``. None for a longer chain or any other callee (``this.api.get``,
    ``axios.defaults.get``)."""
    fn = call.child_by_field_name("function")
    if fn is not None and fn.type == "identifier":
        return node_text(fn, source)
    obj = fn.child_by_field_name("object") \
        if fn is not None and fn.type == "member_expression" else None
    return node_text(obj, source) if obj is not None and obj.type == "identifier" else None


def shadowed_names(root: Node, source: bytes) -> frozenset[str]:
    """Names this file binds more than once (an import and a local, two locals, a function and a
    variable) or as a parameter: a call through one of them may not reach what one binding says."""
    counts: dict[str, int] = {}
    params: set[str] = set()

    def bind(node: Node | None) -> None:
        if node is not None and node.type == "identifier":
            name = node_text(node, source)
            counts[name] = counts.get(name, 0) + 1

    def walk(n: Node) -> None:
        if n.type in ("variable_declarator", "function_declaration", "class_declaration",
                      "generator_function_declaration"):
            bind(n.child_by_field_name("name"))
        elif n.type == "import_clause":
            for c in n.named_children:
                if c.type == "identifier":
                    bind(c)
                elif c.type == "namespace_import":
                    for i in c.named_children:
                        bind(i)
                elif c.type == "named_imports":
                    for spec in c.named_children:
                        bind(spec.child_by_field_name("alias") or spec.child_by_field_name("name"))
        elif n.type == "formal_parameters":
            for p in n.named_children:
                target = p.child_by_field_name("pattern") or p.child_by_field_name("left") or p
                if target.type == "identifier":
                    params.add(node_text(target, source))
        elif n.type == "arrow_function":
            single = n.child_by_field_name("parameter")
            if single is not None and single.type == "identifier":
                params.add(node_text(single, source))
        for c in n.named_children:
            walk(c)

    walk(root)
    return frozenset(params | {n for n, k in counts.items() if k > 1})
