"""Flat statement capture for C# (gated by statement capture) + shared API/DB
call detection."""

from __future__ import annotations

import contextvars
from collections.abc import Mapping

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..statements_common import (
    classify_statement,
    render_concat,
    reset_datastore_receivers,
    reset_http_client_ids,
    resolve_endpoint,
    set_datastore_receivers,
    set_http_client_ids,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .attributes import extract_attributes
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES
from .scopes import Binding, Classify, TypeScopes

# C# declaration node types that may carry attributes ([NotMapped], [Required], etc.)
_ATTRIBUTED_DECL_TYPES = {"field_declaration", "property_declaration"}

_CALL_TYPE = "invocation_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_declaration_statement", "field_declaration"):
        vd = next((c for c in node.named_children if c.type == "variable_declaration"), None)
        if vd is not None:
            decl = next((c for c in vd.named_children if c.type == "variable_declarator"), None)
            if decl is not None:
                nm = decl.child_by_field_name("name") or (
                    decl.named_children[0] if decl.named_children else None)
                if nm is not None:
                    return node_text(nm, source)
    elif node.type == "property_declaration":  # `public int Count { get; set; }` -> Count
        nm = node.child_by_field_name("name")
        if nm is not None:
            return node_text(nm, source)
    return None


def method_name(name_node: Node | None, source: bytes) -> str:
    """Method name from an invocation's ``function``/``name`` node, dropping generic
    type arguments (``GetById<Order>`` -> ``GetById``) so db/api classification and
    call-path matching key on the bare name."""
    if name_node is None:
        return ""
    if name_node.type == "generic_name":
        ident = next((c for c in name_node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return node_text(ident, source)
    return node_text(name_node, source)


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, interpolated string (``$"…{x}…"``), or ``+``
    concatenation. Interpolations become ``{name}``; a leading interpolated base is dropped."""
    if node.type == "argument":  # C# wraps each positional arg in an `argument` node
        inner = node.named_children[0] if node.named_children else None
        return _render_url(inner, source) if inner is not None else None
    if node.type == "string_literal":
        content = next((c for c in node.named_children if c.type == "string_literal_content"), None)
        return node_text(content, source) if content is not None else node_text(node, source).strip('"')
    if node.type == "interpolated_string_expression":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(node_text(c, source))
            elif c.type == "interpolation":
                expr = next((x for x in c.named_children if x.type != "interpolation_brace"), None)
                parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
        return strip_leading_base("".join(parts))
    if node.type == "binary_expression":  # "/a/" + id
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    func = call.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "member_access_expression":
        name_node = func.child_by_field_name("name")
        obj = func.child_by_field_name("expression")
        method = method_name(name_node, source)
        callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    else:
        method = method_name(func, source)
        callee = method
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    request = _request_message(named, source)
    if request is not None:  # client.SendAsync(new HttpRequestMessage(HttpMethod.Post, url))
        verb, url_node = request
        endpoint = _render_url(url_node, source) if url_node is not None else None
        return callee, verb or method, endpoint
    endpoint, override = resolve_endpoint(named, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _request_message(args: list[Node], source: bytes) -> tuple[str | None, Node | None] | None:
    """``new HttpRequestMessage(HttpMethod.Post, url)`` as a call's first argument → (``POST``,
    the url node); ``None`` when the first argument is not one. A verb that is not a literal
    ``HttpMethod.X`` → (None, url): the request is known, its verb is not."""
    first = args[0] if args else None
    inner = first.named_children[0] if first is not None and first.named_children else None
    if inner is None or inner.type != "object_creation_expression":
        return None
    type_node = inner.child_by_field_name("type")
    if type_node is None or node_text(type_node, source).rsplit(".", 1)[-1] != "HttpRequestMessage":
        return None
    ctor = inner.child_by_field_name("arguments")
    ctor_args = [a.named_children[0] for a in ctor.named_children if a.named_children] \
        if ctor is not None else []
    verb = None
    if ctor_args and node_text(ctor_args[0], source).startswith("HttpMethod."):
        verb = node_text(ctor_args[0], source).rsplit(".", 1)[-1].upper()
    return verb, ctor_args[1] if len(ctor_args) > 1 else None


_HTTP_CLIENT_TYPES = {"HttpClient"}


def type_name(typ: str) -> str:
    """The simple name of a declared type: ``Amazon.S3.IAmazonS3?`` → ``IAmazonS3``,
    ``Lazy<T>`` → ``Lazy``."""
    return typ.split("<", 1)[0].rstrip("?").rsplit(".", 1)[-1]


def typed(wanted: Mapping[str, str]) -> Classify:
    """A declaration → the label of its ``wanted`` type (simple type name → label): its declared
    type, or for ``var`` the type it is created as (``var s3 = new AmazonS3Client()``)."""
    def classify(b: Binding) -> str | None:
        if b.type is not None:
            return wanted.get(type_name(b.type))
        if b.init is not None and b.init.type == "object_creation_expression":
            t = b.init.child_by_field_name("type")
            return wanted.get(type_name(_raw(t))) if t is not None else None
        return None
    return classify


def _http_client(b: Binding) -> str | None:
    """``http`` for a declaration that holds an ``HttpClient``: declared with that type, or a
    ``var`` initialised from ``new HttpClient(…)`` / ``factory.CreateClient(…)``."""
    if b.type is not None:
        return "http" if type_name(b.type) in _HTTP_CLIENT_TYPES else None
    return "http" if b.init is not None and _creates_client(b.init) else None


def _creates_client(value: Node) -> bool:
    """``new HttpClient(…)`` or ``….CreateClient(…)`` (IHttpClientFactory)."""
    if value.type == "object_creation_expression":
        typ = value.child_by_field_name("type")
        return typ is not None and type_name(_raw(typ)) in _HTTP_CLIENT_TYPES
    if value.type == "invocation_expression":
        fn = value.child_by_field_name("function")
        name = fn.child_by_field_name("name") if fn is not None and \
            fn.type == "member_access_expression" else None
        return name is not None and _raw(name) == "CreateClient"
    return False


def _raw(node: Node) -> str:
    return (node.text or b"").decode("utf-8", "replace")


#: AWS SDK for .NET DynamoDB client types → ``dataAccessHint``. Their names are distinctive, so
#: they count without the file's own import (a project's global usings may supply it).
_DATASTORE_TYPES = {
    "IAmazonDynamoDB": "dynamodb", "AmazonDynamoDBClient": "dynamodb",
    "IDynamoDBContext": "dynamodb", "DynamoDBContext": "dynamodb",
}
#: ``Table`` (the DynamoDB document model) is a common class name: it counts only in a file that
#: imports the DynamoDB SDK.
_DATASTORE_TYPES_WITH_IMPORT = {**_DATASTORE_TYPES, "Table": "dynamodb"}


class ReceiverNames:
    """The names that hold an ``HttpClient`` or a datastore client, for the shared call
    classifier — bound by scope (:mod:`.scopes`), so each body sees its own: ``_ctx`` is a
    DynamoDB context in the class that declares it so, and nothing in the class next to it that
    declares ``_ctx`` as its own unit of work.

    The shared classifier takes names, not nodes, so :func:`extract_statements` sets them
    (``set_http_client_ids`` / ``set_datastore_receivers``) for each body it walks."""

    def __init__(self, root: Node, source: bytes) -> None:
        self.scopes = TypeScopes(root, source)
        self._http = self.scopes.candidates(_http_client)
        self._store: Classify | None = None
        self._stores: frozenset[str] = frozenset()
        if b"DynamoDB" in source:
            self._store = typed(_DATASTORE_TYPES_WITH_IMPORT if b"Amazon.DynamoDBv2" in source
                                else _DATASTORE_TYPES)
            self._stores = self.scopes.candidates(self._store)

    def __bool__(self) -> bool:
        return bool(self._http or self._stores)

    def at(self, node: Node) -> tuple[frozenset[str], dict[str, str]]:
        """(HttpClient names, datastore name → product) as bound at ``node``."""
        http = frozenset(self.scopes.labels_at(node, _http_client, self._http)) \
            if self._http else frozenset()
        stores = self.scopes.labels_at(node, self._store, self._stores) \
            if self._store is not None and self._stores else {}
        return http, stores


#: The file's receiver names while it is extracted (set by the C# parser; reset in a finally).
_receiver_names: contextvars.ContextVar[ReceiverNames | None] = contextvars.ContextVar(
    "csharp_receiver_names", default=None)


def set_receiver_names(names: ReceiverNames | None) -> "contextvars.Token[ReceiverNames | None]":
    return _receiver_names.set(names)


def reset_receiver_names(token: "contextvars.Token[ReceiverNames | None]") -> None:
    _receiver_names.reset(token)


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def _iter_in_scope(node: Node, descend_all: bool = False, barriers: frozenset[tuple[int, int]] = frozenset()):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas/anonymous methods, attributing their statements to this
    function, EXCEPT nested local functions (their spans are in ``barriers``) — those
    are extracted as their own scope. ``False`` (file-root / class-body) keeps nested
    scopes as barriers since they are extracted as their own Function/Class."""
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child, descend_all, barriers)


def _field_attributes(node: Node, source: bytes) -> list[Decorator] | None:
    """Structured decorators (C# attributes) on a field/property node, via the shared
    ``extract_attributes`` (same parser used for class/method/param attributes)."""
    if node.type not in _ATTRIBUTED_DECL_TYPES:
        return None
    return extract_attributes(node, source)


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    names = _receiver_names.get()
    if not names:
        return _extract(body, source, path, parent_id, limit, seen_ids, descend_all, barriers)
    http, stores = names.at(body)
    token, store_token = set_http_client_ids(http), set_datastore_receivers(stores or None)
    try:
        return _extract(body, source, path, parent_id, limit, seen_ids, descend_all, barriers)
    finally:
        reset_datastore_receivers(store_token)
        reset_http_client_ids(token)


def _extract(body: Node, source: bytes, path: str, parent_id: str, limit: int,
             seen_ids: set[str], descend_all: bool,
             barriers: frozenset[tuple[int, int]]) -> list[Statement]:
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details, language="csharp",
                decorators=_field_attributes(node, source),
            )
        )
    return out
