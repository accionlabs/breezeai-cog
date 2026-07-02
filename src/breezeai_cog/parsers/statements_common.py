"""Shared statement-record emission for --capture-statements (all languages).

The per-language ``statements.py`` yields statement nodes (``_iter_in_scope``) and
supplies a language-specific ``name_of`` + ``call_details`` + call node type; this
module turns one statement node into its ``Statement`` record(s):

  * a **base** structural record (``nodeType`` = the AST node), carrying the *first*
    api/db/query hit found in the statement's own expression — backward-compatible with
    the single-classification model, and
  * one **synthetic** same-span record per *additional* hit (``nodeType`` = the call
    node type, following the annotation-route precedent) so method chains and
    multi-call expressions don't lose every hit after the first (spec item #4).

Calls are gathered by ``_iter_calls``, which walks the statement's own expression but
**stops at nested EMIT_TYPES statements** (those are emitted and classified on their
own — this is also what keeps a call nested in an ``if``/``for``/``try`` *body* from
being mis-attributed to the control statement). It does *not* stop at scopes, so a
concise-bodied lambda (``x => repo.save(x)``) is still classified here.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator

from tree_sitter import Node

from ..emit import disambiguate, statement_id
from ..schemas import Statement
from ..utils import truncate
from .detection import classify_call, text_has_query
from .treesitter import first_line, node_text

# (callee, method, first_string_arg) or None for a single call node.
CallDetails = Callable[[Node, bytes], "tuple[str, str, str | None] | None"]
NameOf = Callable[[Node, bytes], "str | None"]


def _iter_calls(
    node: Node,
    emit_types: Collection[str],
    call_type: str,
    stmt_expr: Collection[str],
    containers: Collection[str],
) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in emit_types:
            continue  # a nested statement — classified on its own
        if child.type in stmt_expr and node.type in containers:
            continue  # a bare statement-position expression (its own statement — Python)
        if child.type == call_type:
            yield child
        yield from _iter_calls(child, emit_types, call_type, stmt_expr, containers)


def _calls_in_statement(
    node: Node,
    emit_types: Collection[str],
    call_type: str,
    stmt_expr: Collection[str],
    containers: Collection[str],
) -> Iterator[Node]:
    # The statement node may itself be a call — a bare Python call-statement
    # (``session.add(x)``) has no expression-statement wrapper.
    if node.type == call_type:
        yield node
    yield from _iter_calls(node, emit_types, call_type, stmt_expr, containers)


def classify_statement(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    limit: int,
    seen_ids: set[str],
    emit_types: Collection[str],
    control_flow: Collection[str],
    call_type: str,
    name_of: NameOf,
    call_details: CallDetails,
    stmt_expr: Collection[str] = (),
    container_types: Collection[str] = (),
) -> list[Statement]:
    text = node_text(node, source)
    if node.type in control_flow:
        text = first_line(text)
    start, col = node.start_point[0] + 1, node.start_point[1]
    end = node.end_point[0] + 1

    # All api/db/query hits in this statement's own expression, deduped by (kind, method).
    hits: list[tuple[str, str, str | None, str | None, Node]] = []
    seen_hit: set[tuple[str, str]] = set()
    for call in _calls_in_statement(node, emit_types, call_type, stmt_expr, container_types):
        det = call_details(call, source)
        if det is None:
            continue
        classified = classify_call(det[0], det[1], det[2])
        if classified is None:
            continue
        sem, meth, dh = classified
        key = (sem, meth)
        if key in seen_hit:
            continue
        seen_hit.add(key)
        ep = det[2] if sem == "api_call" else None
        hits.append((sem, meth, ep, dh, call))

    records: list[Statement] = []
    if hits:
        semantic, method_value, endpoint, hint, _ = hits[0]
    else:
        semantic = method_value = endpoint = hint = None
        if text_has_query(text):  # raw SQL/Cypher string literal, no classified call
            semantic = "query_statement"
    records.append(
        Statement(
            id=disambiguate(statement_id(path, start, col), seen_ids),
            parentId=parent_id,
            nodeType=node.type,
            semanticType=semantic,
            text=truncate(text, limit),
            name=name_of(node, source),
            method=method_value,
            endpoint=endpoint,
            dataAccessHint=hint,
            startLine=start,
            endLine=end,
            path=path,
        )
    )
    # One synthetic record per additional hit, at the same span (fields stay single-valued).
    for semantic, method_value, endpoint, hint, call in hits[1:]:
        cs, ccol = call.start_point[0] + 1, call.start_point[1]
        records.append(
            Statement(
                id=disambiguate(statement_id(path, cs, ccol), seen_ids),
                parentId=parent_id,
                nodeType=call.type,
                semanticType=semantic,
                text=truncate(node_text(call, source), limit),
                method=method_value,
                endpoint=endpoint,
                dataAccessHint=hint,
                startLine=start,
                endLine=end,
                path=path,
            )
        )
    return records
