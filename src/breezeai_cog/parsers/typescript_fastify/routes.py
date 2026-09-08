from __future__ import annotations
 
import logging
from typing import Optional
 
from tree_sitter import Node
 
from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import node_text
 
__all__ = ["detect_fastify_routes"]
 
logger = logging.getLogger(__name__)
 
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}
_ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
 
_CALLABLE_NODE_TYPES = {
    "arrow_function",
    "function_expression",
    "function",
    "identifier",
    "member_expression",
}
 
_STRING_NODE_TYPES = {"string", "template_string"}
 
# Tree-sitter's node type for `{ prefix }` object-literal shorthand, where
# the key and the value are the same identifier (`{ prefix }` means
# `{ prefix: prefix }`). Grammar versions vary slightly on the exact name,
# so both are checked.
_SHORTHAND_PROPERTY_TYPES = {
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
}

_IDENTIFIER_LIKE_TYPES = {"identifier"} | _SHORTHAND_PROPERTY_TYPES
 
_SCOPE_NODE_TYPES = {"program", "statement_block"}
 
def _walk(node: Node,*,source: bytes,path: str,fid: str,fn_by_name: dict,seen_ids: set[str],out: list[Statement],prefix: str) -> None:
    if node.type == "call_expression":
        dispatch = _dispatch_call(node, source)
        if dispatch is not None:
            kind, member_name, args = dispatch
 
            if kind == "method":
                _http_method_route(
                    node, member_name, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    seen_ids=seen_ids, out=out,
                )
 
            elif kind == "app_route":
                _app_route(
                    node, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    seen_ids=seen_ids, out=out,
                )
 
            elif kind == "register":
                _fastify_register(
                    node, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    seen_ids=seen_ids, out=out,
                )
                return
 
    for child in node.children:
        _walk(
            child, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            seen_ids=seen_ids, out=out, prefix=prefix,
        )
 
 
def _dispatch_call(node: Node, source: bytes) -> Optional[tuple[str, str, list[Node]]]:
    callee = node.child_by_field_name("function")
    if callee is None or callee.type != "member_expression":
        return None
 
    prop = callee.child_by_field_name("property")
    if prop is None:
        return None
    member_name = node_text(prop, source)
 
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None
    args = list(args_node.named_children)
 
    if member_name in _HTTP_METHODS:
        return "method", member_name, args
    if member_name == "route":
        return "app_route", member_name, args
    if member_name == "register":
        return "register", member_name, args
    return None
 
def _http_method_route(node: Node,member_name: str,args: list[Node],prefix: str,*,source: bytes,path: str,fid: str,
    fn_by_name: dict,
    seen_ids: set[str],
    out: list[Statement],
) -> None:
    if len(args) < 2:
        return
 
    url = _resolve_static_string(args[0], source)
    if url is None or not url.startswith("/"):
        return
 
    handler_node = args[-1]
    if handler_node.type not in _CALLABLE_NODE_TYPES:
        return
 
    methods = _ALL_METHODS if member_name == "all" else [member_name.upper()]
    route = _join_prefix(prefix, url)
    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node, source, fid=fid, fn_by_name=fn_by_name
    )
 
    for m in methods:
        out.append(
            _make_statement(
                node,
                method=m,
                endpoint=route,
                parent_id=parent_id,
                handler=handler_name,
                handler_line=handler_line,
                semantic_type="route",
                route_kind="route",
                path=path,
                source=source,
                seen_ids=seen_ids,
            )
        )
 
def _app_route(
    node: Node,
    args: list[Node],
    prefix: str,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict,
    seen_ids: set[str],
    out: list[Statement],
) -> None:
    if not args or args[0].type != "object":
        return
 
    pairs = _object_pairs(args[0], source)
    method_node = pairs.get("method")
    url_node = pairs.get("url") or pairs.get("path")
    handler_node = pairs.get("handler")
 
    if url_node is None:
        return
    url = _resolve_static_string(url_node, source)
    if url is None or not url.startswith("/"):
        return
 
    if method_node is None:
        methods, dynamic = ["GET"], False
    else:
        methods, dynamic = _method_values(method_node, source)
 
    if dynamic:
        logger.warning(
            "fastify: could not statically resolve one or more HTTP "
            "methods for route %s at %s:%d; keeping any resolvable "
            "method(s) and adding UNKNOWN for the rest instead of "
            "dropping them silently",
            url, path, node.start_point[0] + 1,
        )
        methods.append("UNKNOWN")
 
    if not methods:
        return
 
    route = _join_prefix(prefix, url)
    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node, source, fid=fid, fn_by_name=fn_by_name
    )
 
    for m in methods:
        out.append(
            _make_statement(
                node,
                method=m,
                endpoint=route,
                parent_id=parent_id,
                handler=handler_name,
                handler_line=handler_line,
                semantic_type="route",
                route_kind="route",
                path=path,
                source=source,
                seen_ids=seen_ids,
            )
        )
 
def _method_values(method_node: Node, source: bytes) -> tuple[list[str], bool]:
    if method_node.type in _STRING_NODE_TYPES:
        value = _string_literal(method_node, source)
        return ([value.upper()] if value else []), False
 
    if method_node.type == "array":
        methods: list[str] = []
        dynamic = False
        for child in method_node.named_children:
            v = _resolve_static_string(child, source)
            if v:
                methods.append(v.upper())
            else:
                dynamic = True
        return methods, dynamic
 
    # identifier, shorthand property, member_expression, call_expression,
    # conditional, etc.
    resolved = _resolve_static_string(method_node, source)
    if resolved:
        return [resolved.upper()], False
    return [], True
 
def _fastify_register(
    node: Node,
    args: list[Node],
    prefix: str,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict,
    seen_ids: set[str],
    out: list[Statement],
) -> None:
    if not args:
        return
 
    plugin_node = args[0]
    opts_node = args[1] if len(args) > 1 else None
 
    child_prefix = prefix
    mount_endpoint = prefix or "/"
    if opts_node is not None and opts_node.type == "object":
        pairs = _object_pairs(opts_node, source)
        prefix_node = pairs.get("prefix")
        if prefix_node is not None:
            declared = _resolve_static_string(prefix_node, source)
            if declared:
                child_prefix = _join_prefix(prefix, declared)
                mount_endpoint = child_prefix
            else:
                child_prefix = _join_prefix(prefix, "<unresolved-prefix>")
                mount_endpoint = child_prefix
                logger.warning(
                    "fastify: register() prefix at %s:%d is not a static "
                    "string; this mount and every route nested inside it "
                    "will show '<unresolved-prefix>' in their endpoint "
                    "instead of silently omitting the segment",
                    path, node.start_point[0] + 1,
                )
 
    parent_id, handler_name, handler_line = _resolve_handler(
        plugin_node, source, fid=fid, fn_by_name=fn_by_name
    )
    out.append(
        _make_statement(
            node,
            method="MOUNT",
            endpoint=mount_endpoint,
            parent_id=parent_id,
            handler=handler_name,
            handler_line=handler_line,
            semantic_type="mount",
            route_kind="mount",
            path=path,
            source=source,
            seen_ids=seen_ids,
        )
    )
 
    # Compose the prefix into anything registered inside an inline plugin.
    if plugin_node.type in ("arrow_function", "function_expression", "function"):
        _walk(
            plugin_node, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            seen_ids=seen_ids, out=out, prefix=child_prefix,
        )
 
    # Still walk the options object at the *outer* prefix in case it hides
    # unrelated nested calls (rare, but cheap to cover).
    if opts_node is not None:
        _walk(
            opts_node, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            seen_ids=seen_ids, out=out, prefix=prefix,
        )
 
 
def _join_prefix(prefix: str, suffix: str) -> str:
    prefix = prefix.rstrip("/")
    if not suffix or suffix == "/":
        return prefix or "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"{prefix}{suffix}"
 
def _resolve_handler(node: Optional[Node], source: bytes, *, fid: str, fn_by_name: dict) -> tuple[str, str, Optional[int]]:
    """Resolve a handler-ish node to (parentId, displayName, handlerLine).
 
    Only a bare identifier can match an extracted top-level function by
    name -- member expressions, inline functions, and unresolved names all
    fall back to the file id rather than fabricating a function id with no
    backing node, mirroring the Next.js detector's honest-null rule.
    """
    if node is None:
        return fid, "<unknown>", None
 
    if node.type == "identifier":
        name = node_text(node, source)
        fn = fn_by_name.get(name)
        if fn is not None:
            return fn.id, name, fn.startLine
        return fid, name, node.start_point[0] + 1
 
    if node.type == "member_expression":
        return fid, node_text(node, source), node.start_point[0] + 1
 
    if node.type in ("arrow_function", "function_expression", "function"):
        return fid, "<inline>", node.start_point[0] + 1
 
    return fid, node_text(node, source)[:80], node.start_point[0] + 1
 
 
def _make_statement(
    node: Node,
    *,
    method: str,
    endpoint: str,
    parent_id: str,
    handler: str,
    handler_line: Optional[int],
    semantic_type: str,
    route_kind: str,
    path: str,
    source: bytes,
    seen_ids: set[str],
) -> Statement:
    sl, sc = node.start_point[0] + 1, node.start_point[1]
    return Statement(
        id=disambiguate(statement_id(path, sl, sc), seen_ids),
        parentId=parent_id,
        semanticType=semantic_type,
        method=method,
        endpoint=endpoint,
        routeKind=route_kind,
        nodeType="synthetic",
        text=node_text(node, source).split("\n", 1)[0],
        framework="fastify",
        handler=handler,
        handlerLine=handler_line,
        isRegex=False,
        authRequired=False,  # Fastify has no decorator/guard concept to inspect here
        guards=None,
        startLine=sl,
        endLine=node.end_point[0] + 1,
        path=path,
    )
 
def _object_pairs(obj_node: Node, source: bytes) -> dict[str, Node]:
    """Map key text -> value node for a `{ key: value, ... }` object
    literal, including shorthand properties: `{ prefix }` is equivalent to
    `{ prefix: prefix }`, and is a *different* tree-sitter node type
    (`shorthand_property_identifier`) than a regular `pair` -- without
    handling it explicitly, shorthand keys are invisible to this lookup
    even though they're extremely common in real code.
    """
    pairs: dict[str, Node] = {}
    for child in obj_node.named_children:
        if child.type == "pair":
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")
            if key_node is None or value_node is None:
                continue
            key_text = node_text(key_node, source).strip("'\"`")
            pairs[key_text] = value_node
        elif child.type in _SHORTHAND_PROPERTY_TYPES:
            # Key and value are the same identifier.
            pairs[node_text(child, source)] = child
    return pairs
 
 
def _resolve_static_string(node: Node, source: bytes) -> Optional[str]:
    literal = _string_literal(node, source)
    if literal is not None:
        return literal
    if node.type in _IDENTIFIER_LIKE_TYPES:
        return _resolve_in_enclosing_scope(node, node_text(node, source), source)
    return None
 
 
def _resolve_in_enclosing_scope(node: Node, name: str, source: bytes) -> Optional[str]:
    """Walk up from `node` through enclosing function bodies to the module
    scope, returning the value of the nearest `const/let/var NAME = '...'`
    found -- nearest (innermost) scope wins, matching normal shadowing."""
    scope = node.parent
    while scope is not None:
        if scope.type in _SCOPE_NODE_TYPES:
            value = _lookup_const_in_block(scope, name, source)
            if value is not None:
                return value
        scope = scope.parent
    return None
 
 
def _lookup_const_in_block(block: Node, name: str, source: bytes) -> Optional[str]:
    """Look for `const/let/var NAME = 'literal'` declared directly in this
    block (not inside a nested block within it)."""
    for child in block.named_children:
        if child.type not in ("lexical_declaration", "variable_declaration"):
            continue
        for declarator in child.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value_node = declarator.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and node_text(name_node, source) == name
                and value_node is not None
            ):
                literal = _string_literal(value_node, source)
                if literal is not None:
                    return literal
    return None
 
 
def _string_literal(node: Node, source: bytes) -> Optional[str]:
    """Literal contents of a string/template-string node, or None if it
    isn't statically known (e.g. a template literal with `${...}`)."""
    if node.type == "string":
        text = node_text(node, source)
        return text[1:-1] if len(text) >= 2 else None
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.children):
            return None
        text = node_text(node, source)
        return text[1:-1] if len(text) >= 2 else None
    return None

def detect_fastify_routes(root: Node,source: bytes,path: str,record: FileRecord,*,seen_ids: set[str]) -> list[Statement]:
    """Walk `root` and return every Fastify route/mount statement found.
 
    `seen_ids` is shared/mutated across calls (via `disambiguate`) so
    duplicate ids never collide across files or passes.
    """
    fid = file_id(path)
    fn_by_name = {f.name: f for f in record.functions if f.parentId == fid}
 
    statements: list[Statement] = []
    _walk(
        root,
        source=source,
        path=path,
        fid=fid,
        fn_by_name=fn_by_name,
        seen_ids=seen_ids,
        out=statements,
        prefix="",
    )
    return statements