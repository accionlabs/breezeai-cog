"""Apollo GraphQL client calls (``this.apollo.query/mutate(...)``), for TypeScript. See
``_detect_apollo_calls``."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from ..typescript_sdk_common import annotation_type, emit_outbound, this_member_call, walk_calls

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
            if annotation_type(n, source) in _APOLLO_ACCESSOR_TYPES:
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
            if annotation_type(n, source) in _APOLLO_CLIENT_TYPES:
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
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    out: list[Emit],
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
    for call in walk_calls(root):
        # Pattern A: this.directField.method(...)
        if direct_fields:
            result = this_member_call(call, source)
            if result is not None:
                field, method = result
                if field in direct_fields and method in _APOLLO_METHODS:
                    endpoint = _apollo_endpoint(call, source) or f"{field}.{method}"
                    emit_outbound(call, endpoint, "graphql", out)
                    emitted = True
                    continue

        # Pattern B: this.accessorField.namedClient.method(...)
        if accessor_fields:
            deep = _this_deep_call(call, source)
            if deep is not None:
                outer, inner, method = deep
                if outer in accessor_fields and method in _APOLLO_METHODS:
                    endpoint = _apollo_endpoint(call, source) or f"{inner}.{method}"
                    emit_outbound(call, endpoint, "graphql", out)
                    emitted = True

    return emitted


def _detect(dc: DetectContext) -> Detection | None:
    out: list[Emit] = []
    if not _detect_apollo_calls(dc.root, dc.source, dc.path, dc.record, out):
        return None
    return Detection(out, label="graphql")


register_detector(Detector(
    name="apollo", gate=Gate("typescript", order=33), detect=_detect,
    frameworks=("graphql",),
))
