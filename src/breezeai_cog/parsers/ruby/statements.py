"""Flat Ruby statement capture."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from tree_sitter import Node

from ...schemas import Statement
from ..detection import text_has_query
from ..statements_common import (
    classify_statement,
    render_concat,
    resolve_endpoint,
)
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES

_SCOPE_TYPES = {"class", "module", "method", "singleton_method"}
_ACTIVE_RECORD_METHODS = frozenset({
    "find_by", "find_by!", "find_each", "find_in_batches", "where", "where_not",
    "joins", "includes", "pluck", "destroy_all", "update_all", "left_outer_joins", "take",
    "find", "all", "create", "create!", "first", "last", "exists",
    "save", "save!", "update", "update!", "destroy", "destroy!", "update_attributes",
})


class _ClassifierDbIdView(frozenset[str]):
    """Membership view combining file-wide model IDs with one scope's additional IDs."""

    _base: frozenset[str]
    _extra: frozenset[str]

    def __new__(cls, base: frozenset[str], extra: frozenset[str]) -> _ClassifierDbIdView:
        view = super().__new__(cls)
        view._base = base
        view._extra = extra
        return view

    def __contains__(self, value: object) -> bool:
        return value in self._base or value in self._extra

    def __iter__(self) -> Iterator[str]:
        yield from self._base
        yield from (name for name in self._extra if name not in self._base)

    def __len__(self) -> int:
        return len(self._base) + sum(name not in self._base for name in self._extra)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (set, frozenset)):
            return len(self) == len(other) and all(name in other for name in self)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(frozenset(self))


def _expand_classifier_db_ids(receiver_ids: Iterable[str]) -> frozenset[str]:
    return frozenset(
        name
        for receiver in receiver_ids
        for name in (receiver, receiver.rsplit(".", 1)[-1])
    )


def classifier_model_ids(model_ids: Iterable[str]) -> frozenset[str]:
    """Build the reusable classifier IDs for the models visible in one Ruby file."""
    return _expand_classifier_db_ids(model_ids)


def _iter_statements(node: Node, source: bytes) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in EMIT_TYPES or (
            child.type == "string"
            and node.type in {"program", "body_statement"}
            and text_has_query(node_text(child, source))
        ):
            yield child
        if child.type in _SCOPE_TYPES:
            continue
        yield from _iter_statements(child, source)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None:
            return node_text(left, source)
    if node.type in {"class", "module"}:
        constant = next((c for c in node.named_children if c.type == "constant"), None)
        if constant is not None:
            return node_text(constant, source)
    if node.type == "method":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return node_text(ident, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if _contains_string_interpolation(node):
        return None
    if node.type == "string":
        parts = node.named_children
        if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
            return None
        return "".join(node_text(part, source) for part in parts)
    if node.type == "binary":
        return render_concat(node, source, _render_url)
    return None


def _contains_string_interpolation(node: Node) -> bool:
    """Return True when this URL expression contains Ruby string interpolation."""
    return node.type == "interpolation" or any(
        _contains_string_interpolation(child) for child in node.named_children
    )


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    receiver = call.child_by_field_name("receiver")
    method_node = call.child_by_field_name("method")
    method = node_text(method_node, source) if method_node is not None else ""
    callee = f"{node_text(receiver, source)}.{method}" if receiver is not None else method
    arguments = call.child_by_field_name("arguments")
    args = list(arguments.named_children) if arguments is not None else []
    endpoint, override = resolve_endpoint(args, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _call_details_with_redis_evidence(
    call: Node, source: bytes, typed_db_ids: frozenset[str] | None = None
) -> tuple[str, str, str | None] | None:
    details = _call_details(call, source)
    if details is None:
        return None
    receiver = call.child_by_field_name("receiver")
    receiver_text = node_text(receiver, source).lower() if receiver is not None else ""
    method = details[1].lower()
    # Ruby-local exclusions for ordinary methods that collide with HTTP verbs in the
    # shared classifier. Keep the shared, cross-language classifier unchanged.
    if method == "fetch" and receiver is None:
        return None
    if method == "delete" and receiver_text.split(".")[-1].lstrip("@") in {
        "session", "cookies",
    }:
        return None
    if receiver_text.endswith("request.session") and method == "options":
        return None
    if method == "fetch" and receiver is not None:
        return None
    if details[1].lower() in _ACTIVE_RECORD_METHODS:
        receiver_name = node_text(receiver, source) if receiver is not None else ""
        if typed_db_ids is None or receiver_name not in typed_db_ids:
            return None
    if receiver is not None:
        receiver_name = node_text(receiver, source)
        terminal = receiver_name.rsplit(".", 1)[-1].lower().lstrip("@")
        if terminal in {"cache", "cachemanager", "cacheservice"} or terminal.endswith("cache"):
            return None
        if terminal.replace("_", "") in {"redis", "redisclient", "redisservice"} and not (
            re.search(rb"(?m)^\s*require\s*[\(\s]*['\"]redis['\"]", source)
            or re.search(rb"\bRedis\s*\.\s*new\b", source)
        ):
            return None
    return details

def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    typed_db_ids: frozenset[str] | None = None,
    classifier_db_ids: frozenset[str] | None = None,
    extra_typed_db_ids: frozenset[str] | None = None,
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    if typed_db_ids is None:
        scoped_classifier_ids = None
    elif classifier_db_ids is None:
        scoped_classifier_ids = _expand_classifier_db_ids(typed_db_ids)
    elif extra_typed_db_ids:
        scoped_classifier_ids = _ClassifierDbIdView(
            classifier_db_ids, _expand_classifier_db_ids(extra_typed_db_ids)
        )
    else:
        scoped_classifier_ids = classifier_db_ids
    for node in _iter_statements(body, source):
        if node.type not in EMIT_TYPES and not (
            node.type == "string" and text_has_query(node_text(node, source))
        ):
            continue
        classified = classify_statement(
            node,
            source,
            path,
            parent_id=parent_id,
            limit=limit,
            seen_ids=seen_ids,
            emit_types=EMIT_TYPES,
            control_flow=CONTROL_FLOW,
            call_type="call",
            name_of=_name_of,
            call_details=lambda call, src: _call_details_with_redis_evidence(
                call, src, typed_db_ids
            ),
            language="ruby",
            typed_db_ids=scoped_classifier_ids,
        )
        # A wrapper/DSL block is not itself a database query because SQL text occurs
        # somewhere in its body. Its inner SQL statement is visited independently.
        if node.type == "call" and any(
            node_text(node.child_by_field_name("method"), source) == dsl
            for dsl in ("safety_assured", "class_methods")
            if node.child_by_field_name("method") is not None
        ):
            classified = [s for s in classified if s.semanticType != "query_statement"]
        # Only Ruby-local evidence supports ActiveRecord and Redis here. The generic
        # shared classifier's vendor-name hints do not establish another Ruby DB client.
        classified = [
            s for s in classified
            if s.dataAccessHint in {None, "activerecord", "redis"}
        ]
        out.extend(classified)
    return out
