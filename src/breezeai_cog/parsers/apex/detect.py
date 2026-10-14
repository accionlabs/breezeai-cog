"""Semantic classification of Apex call-like nodes (SOQL/SOSL, DML, callouts, jobs, events).

The shared detectors key off Java/JS-style method names and quoted SQL, so they miss
Apex's inline ``[SELECT …]``, DML keywords (``insert acc;``), ``Database.insert`` and
``Http.send``. This module classifies those directly from the syntax tree. Names are
compared in lower case because Apex is case-insensitive.
"""

from __future__ import annotations

import contextvars

from tree_sitter import Node

from ..statements_common import render_concat
from ..treesitter import node_text

#: ``(semanticType, method, endpoint, dataAccessHint)``
Hit = tuple[str, str | None, str | None, str | None]

CALL_TYPES = frozenset({"method_invocation", "dml_expression", "query_expression"})

_DML_METHODS = frozenset({"insert", "update", "upsert", "delete", "undelete", "merge"})
_QUERY_METHODS = frozenset({
    "query", "countquery", "getquerylocator", "querywithbinds",
    "countquerywithbinds", "getquerylocatorwithbinds",
})
_DATA_ACCESS_HINT = "salesforce"
_FUNCTION_SCOPES = ("method_declaration", "constructor_declaration", "trigger_declaration")

#: Lower-cased variable name -> lower-cased declared type for the file being extracted, so
#: ``h.send(req)`` can be recognised as an ``Http`` call. Set by the parser around a file
#: and reset afterwards so a reused worker process never sees the previous file's types.
_var_types: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "apex_var_types", default={}
)


def set_var_types(types: dict[str, str]) -> "contextvars.Token[dict[str, str]]":
    return _var_types.set(types)


def reset_var_types(token: "contextvars.Token[dict[str, str]]") -> None:
    _var_types.reset(token)


def _receiver(call: Node, source: bytes) -> str:
    obj = call.child_by_field_name("object")
    return node_text(obj, source).lower() if obj is not None else ""


def _is_http_receiver(call: Node, source: bytes) -> bool:
    obj = call.child_by_field_name("object")
    if obj is None:
        return False
    if obj.type == "object_creation_expression":
        typ = obj.child_by_field_name("type")
        return typ is not None and node_text(typ, source).lower() == "http"
    return _var_types.get().get(node_text(obj, source).lower()) == "http"


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "string_literal":
        return node_text(node, source).strip("'")
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _enclosing_function(node: Node) -> Node | None:
    cur = node.parent
    while cur is not None and cur.type not in _FUNCTION_SCOPES:
        cur = cur.parent
    return cur


def _request_setting(call: Node, source: bytes, setter: str) -> Node | None:
    """The first argument of ``<request>.<setter>(…)`` in the same function, where
    ``<request>`` is the argument passed to ``send``."""
    args = call.child_by_field_name("arguments")
    scope = _enclosing_function(call)
    if args is None or scope is None or not args.named_children:
        return None
    request = node_text(args.named_children[0], source).lower()
    stack = [scope]
    while stack:
        node = stack.pop()
        if node.type == "method_invocation":
            name = node.child_by_field_name("name")
            obj = node.child_by_field_name("object")
            if (
                name is not None and obj is not None
                and node_text(name, source).lower() == setter
                and node_text(obj, source).lower() == request
            ):
                call_args = node.child_by_field_name("arguments")
                if call_args is not None and call_args.named_children:
                    return call_args.named_children[0]
        stack.extend(reversed(node.named_children))
    return None


def _callout(call: Node, source: bytes) -> Hit:
    method_arg = _request_setting(call, source, "setmethod")
    verb = _render_url(method_arg, source) if method_arg is not None else None
    endpoint_arg = _request_setting(call, source, "setendpoint")
    endpoint = _render_url(endpoint_arg, source) if endpoint_arg is not None else None
    return "api_call", verb.upper() if verb else None, endpoint, None


def classify_node(node: Node, source: bytes) -> Hit | None:
    if node.type == "query_expression":
        kind = "sosl" if any(c.type == "sosl_query_body" for c in node.named_children) else "soql"
        return "query_statement", kind, None, None
    if node.type == "dml_expression":
        dml = next((c for c in node.named_children if c.type == "dml_type"), None)
        verb = node_text(dml, source).lower() if dml is not None else ""
        return ("db_method_call", verb, None, _DATA_ACCESS_HINT) if verb else None
    if node.type != "method_invocation":
        return None
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name, receiver = node_text(name_node, source).lower(), _receiver(node, source)
    if receiver == "database":
        if name in _QUERY_METHODS:
            return "query_statement", name, None, None
        if name in _DML_METHODS:
            return "db_method_call", name, None, _DATA_ACCESS_HINT
    elif receiver == "search" and name == "query":
        return "query_statement", name, None, None
    elif receiver == "system" and name == "schedule":
        return "timer", name, None, None
    elif receiver == "eventbus" and name == "publish":
        return "eventbus_publish", name, None, None
    elif name == "send" and _is_http_receiver(node, source):
        return _callout(node, source)
    return None
