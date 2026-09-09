"""Fastify route detection.

Finds Fastify routes in a parsed JS/TS file: `.get()/.post()/...`
shorthand calls, `fastify.route({...})` config objects, and
`fastify.register(plugin, { prefix })` plugin mounts. Prefixes from
nested `register()` calls are combined together, and values written as
variables (including `{ prefix }` shorthand) are looked up where
possible instead of being treated as missing.

This file is independent from the Express and Next.js/Nest.js
detectors -- Fastify's route shapes don't match any of theirs, so
nothing about route detection itself is shared between them.
"""

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

# A bare identifier and an object-literal shorthand key are both "just a
# name" as far as value resolution is concerned -- but tree-sitter gives
# them different node types, so both must be checked explicitly.
_IDENTIFIER_LIKE_TYPES = {"identifier"} | _SHORTHAND_PROPERTY_TYPES

# Block types that introduce a new variable scope: the whole file, and any
# function body. Used to walk "nearest scope outward" when resolving an
# identifier to a `const` declaration actually in scope at that point.
_SCOPE_NODE_TYPES = {"program", "statement_block"}


# Names Fastify's factory function is conventionally imported/called as.
_FASTIFY_FACTORY_NAMES = {"fastify", "Fastify"}


def detect_fastify_routes(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    *,
    seen_ids: set[str],
) -> list[Statement]:
    """Find every Fastify route in a parsed file and return them as Statements.

    Walks the whole file looking for three patterns: `.get()/.post()/...`
    shorthand calls, `.route({...})` config objects, and `.register()`
    plugin mounts. Prefixes from nested `register()` calls are combined
    together, and values written as variables are looked up where
    possible instead of being treated as missing.

    Args:
        root (Node): Root node of the parsed file's syntax tree.
        source (bytes): Raw file contents, used to read text from nodes.
        path (str): File path, used to build ids and file-level lookups.
        record (FileRecord): Already-extracted file record, used to look
            up known top-level functions for handler resolution.
        seen_ids (set[str]): Ids already used elsewhere; updated in place
            so no two Statements ever get the same id.

    Returns:
        list[Statement]: One Statement per route or plugin mount found.
    """
    fid = file_id(path)
    # Authoritative handler ids from the base extraction (top-level
    # functions only) -- same lookup shape as the Next.js detector's
    # `fn_by_name`.
    fn_by_name = {f.name: f for f in record.functions if f.parentId == fid}
    # Identifiers we can positively confirm are bound to a Fastify
    # instance, used to gate the `.get/.post/...` shorthand check against
    # unrelated objects with a similarly-shaped API (see `_dispatch_call`).
    fastify_instance_names = _collect_fastify_instance_names(root, source)

    statements: list[Statement] = []
    _walk(
        root,
        source=source,
        path=path,
        fid=fid,
        fn_by_name=fn_by_name,
        fastify_instance_names=fastify_instance_names,
        seen_ids=seen_ids,
        out=statements,
        prefix="",
    )
    return statements

def _first_param_name(fn_node: Node, source: bytes) -> Optional[str]:
    """Get the name of a function's first parameter, if it's a plain name.
    Args:
        fn_node (Node): The function node to check.
        source (bytes): Raw file contents.
    Returns:
        Optional[str]: The parameter's name, or None if there is no
            first parameter or it isn't a plain name (e.g. destructured).
    """
    params = fn_node.child_by_field_name("parameters")
    if params is None:
        return None
    for child in params.named_children:
        if child.type == "identifier":
            return node_text(child, source)
        return None  # first param exists but isn't a plain name
    return None

def _is_fastify_factory_call(value_node: Node, source: bytes) -> bool:
    """Check whether a value comes from calling the Fastify factory.
    Matches `Fastify(...)`, `fastify(...)`, and `require('fastify')(...)`.
    Args:
        value_node (Node): The node being assigned to a variable.
        source (bytes): Raw file contents.
    Returns:
        bool: True if this value is a Fastify factory call.
    """
    if value_node.type != "call_expression":
        return False
    callee = value_node.child_by_field_name("function")
    if callee is None:
        return False
    if callee.type == "identifier" and node_text(callee, source) in _FASTIFY_FACTORY_NAMES:
        return True
    if callee.type == "call_expression":
        inner_callee = callee.child_by_field_name("function")
        inner_args = callee.child_by_field_name("arguments")
        if (
            inner_callee is not None
            and node_text(inner_callee, source) == "require"
            and inner_args is not None
        ):
            arg_nodes = list(inner_args.named_children)
            if arg_nodes and _string_literal(arg_nodes[0], source) == "fastify":
                return True
    return False

def _collect_fastify_instance_names(root: Node, source: bytes) -> set[str]:
    """Find every variable name known to hold a Fastify instance.
    Looks for two patterns: a variable set to a Fastify factory call, and
    the first parameter of the file's exported plugin function. Anything
    that can't be confirmed this way is simply left out, not guessed at.
    Args:
        root (Node): Root node of the parsed file's syntax tree.
        source (bytes): Raw file contents.
    Returns:
        set[str]: Names confirmed to refer to a Fastify instance.
    """
    names: set[str] = set()

    def walk(node: Node) -> None:
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and value_node is not None
                and _is_fastify_factory_call(value_node, source)
            ):
                names.add(node_text(name_node, source))

        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (
                left is not None
                and node_text(left, source) == "module.exports"
                and right is not None
                and right.type in ("function_expression", "function", "arrow_function")
            ):
                p = _first_param_name(right, source)
                if p:
                    names.add(p)

        if node.type == "export_statement":
            for child in node.named_children:
                if child.type in ("function_declaration", "function"):
                    p = _first_param_name(child, source)
                    if p:
                        names.add(p)

        for child in node.children:
            walk(child)

    walk(root)
    return names

def _walk(node: Node,*,source: bytes,path: str,fid: str,fn_by_name: dict,fastify_instance_names: set[str],
    seen_ids: set[str],
    out: list[Statement],
    prefix: str,
) -> None:
    """Go through every node in the file looking for Fastify route calls.

    Recurses into the whole tree. When a call matches one of the three
    known route shapes, hands it to the matching handler and stops
    recursing into that call's own arguments, so a route's handler body
    isn't re-scanned as if it contained more top-level routes.

    Args:
        node (Node): The current node being checked.
        source (bytes): Raw file contents.
        path (str): File path.
        fid (str): This file's id, used as a fallback parent id.
        fn_by_name (dict): Known top-level functions, by name.
        fastify_instance_names (set[str]): Variable names confirmed to be
            Fastify instances.
        seen_ids (set[str]): Ids already used; updated in place.
        out (list[Statement]): Growing list of results; added to in place.
        prefix (str): URL prefix inherited from any enclosing
            `register()` call.

    Returns:
        None: Results are added to `out` instead of being returned.
    """
    if node.type == "call_expression":
        dispatch = _dispatch_call(node, source, fastify_instance_names)
        if dispatch is not None:
            kind, member_name, args = dispatch

            if kind == "method":
                _http_method_route(
                    node, member_name, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    seen_ids=seen_ids, out=out,
                )
                return

            elif kind == "app_route":
                _app_route(
                    node, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    seen_ids=seen_ids, out=out,
                )
                return  # same reasoning as the "method" branch above.

            elif kind == "register":
                _fastify_register(
                    node, args, prefix,
                    source=source, path=path, fid=fid, fn_by_name=fn_by_name,
                    fastify_instance_names=fastify_instance_names,
                    seen_ids=seen_ids, out=out,
                )
                return

    for child in node.children:
        _walk(
            child, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            fastify_instance_names=fastify_instance_names,
            seen_ids=seen_ids, out=out, prefix=prefix,
        )


    return None

def _dispatch_call(
    node: Node,
    source: bytes,
    fastify_instance_names: set[str],
) -> Optional[tuple[str, str, list[Node]]]:
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
        receiver = callee.child_by_field_name("object")

        if receiver is None or receiver.type != "identifier":
            return None

        receiver_name = node_text(receiver, source)

        if receiver_name not in fastify_instance_names:
            return None

        return "method", member_name, args

    if member_name == "route":
        return "app_route", member_name, args

    if member_name == "register":
        return "register", member_name, args

    return None

def _find_handler_arg(trailing_args: list[Node]) -> Optional[Node]:
    """Find whichever argument after the url looks like the actual handler.

    Checks from the end of the list backwards, since the handler is
    usually last, but doesn't assume it's always in that exact position.

    Args:
        trailing_args (list[Node]): All arguments after the url.

    Returns:
        Optional[Node]: The handler node, or None if nothing looks callable.
    """
    for candidate in reversed(trailing_args):
        if candidate.type in _CALLABLE_NODE_TYPES:
            return candidate
    return None


def _http_method_route(
    node: Node,
    member_name: str,
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
    """Handle a `.get()/.post()/.put()/...` style route call.

    Reads the url and handler out of the call's arguments, applies any
    inherited prefix, and adds one Statement per HTTP method to `out`.

    Args:
        node (Node): The call expression node.
        member_name (str): The method name used in the call, e.g. "get".
        args (list[Node]): The call's arguments.
        prefix (str): URL prefix inherited from any enclosing
            `register()` call.
        source (bytes): Raw file contents.
        path (str): File path.
        fid (str): This file's id, used as a fallback parent id.
        fn_by_name (dict): Known top-level functions, by name.
        seen_ids (set[str]): Ids already used; updated in place.
        out (list[Statement]): Growing list of results; added to in place.

    Returns:
        None: Results are added to `out` instead of being returned.
    """
    if len(args) < 2:
        return

    url = _resolve_static_string(args[0], source)
    if url is None or not url.startswith("/"):
        return

    handler_node = _find_handler_arg(args[1:])
    if handler_node is None:
        logger.warning(
            "fastify: no callable handler argument found for %s %s at "
            "%s:%d; recording it with handler=UNKNOWN instead of "
            "dropping it",
            member_name.upper(), url, path, node.start_point[0] + 1,
        )

    methods = list(_ALL_METHODS) if member_name == "all" else [member_name.upper()]
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
    """Handle a `fastify.route({ method, url, handler })` style call.

    Reads the method, url (or its alias `path`), and handler out of the
    config object, applies any inherited prefix, and adds one Statement
    per HTTP method to `out`. A method that can't be resolved to a real
    value is still recorded as "UNKNOWN" instead of being dropped.

    Args:
        node (Node): The call expression node.
        args (list[Node]): The call's arguments.
        prefix (str): URL prefix inherited from any enclosing
            `register()` call.
        source (bytes): Raw file contents.
        path (str): File path.
        fid (str): This file's id, used as a fallback parent id.
        fn_by_name (dict): Known top-level functions, by name.
        seen_ids (set[str]): Ids already used; updated in place.
        out (list[Statement]): Growing list of results; added to in place.

    Returns:
        None: Results are added to `out` instead of being returned.
    """
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
    """Work out the actual HTTP method(s) for a `.route()` call.

    Handles a single string, a list of strings, or a variable. If any
    value in the list can't be resolved, that's flagged so the caller
    can still record the route instead of silently dropping it.

    Args:
        method_node (Node): The `method` field's value node.
        source (bytes): Raw file contents.

    Returns:
        tuple[list[str], bool]: The resolved method names, and whether
            any value could not be resolved.
    """
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
    fastify_instance_names: set[str],
    seen_ids: set[str],
    out: list[Statement],
) -> None:
    """Handle a `fastify.register(plugin, { prefix })` style call.

    Records a mount Statement for the plugin, and if the plugin is
    written inline in the same file, walks into it so any routes inside
    inherit the combined prefix. If the prefix itself can't be resolved,
    that's flagged in the endpoint and a warning is logged instead of
    silently dropping the prefix.

    Args:
        node (Node): The call expression node.
        args (list[Node]): The call's arguments.
        prefix (str): URL prefix inherited from any enclosing
            `register()` call.
        source (bytes): Raw file contents.
        path (str): File path.
        fid (str): This file's id, used as a fallback parent id.
        fn_by_name (dict): Known top-level functions, by name.
        fastify_instance_names (set[str]): Variable names confirmed to be
            Fastify instances.
        seen_ids (set[str]): Ids already used; updated in place.
        out (list[Statement]): Growing list of results; added to in place.

    Returns:
        None: Results are added to `out` instead of being returned.
    """
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
        child_instance_names = set(fastify_instance_names)
        plugin_param = _first_param_name(plugin_node, source)
        if plugin_param:
            child_instance_names.add(plugin_param)

        _walk(
            plugin_node, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            fastify_instance_names=child_instance_names,
            seen_ids=seen_ids, out=out, prefix=child_prefix,
        )

    # Still walk the options object at the *outer* prefix in case it hides
    # unrelated nested calls (rare, but cheap to cover).
    if opts_node is not None:
        _walk(
            opts_node, source=source, path=path, fid=fid, fn_by_name=fn_by_name,
            fastify_instance_names=fastify_instance_names,
            seen_ids=seen_ids, out=out, prefix=prefix,
        )

def _join_prefix(prefix: str, suffix: str) -> str:
    """Combine a prefix and a url into one clean path.

    Makes sure there's exactly one slash between the two parts, no
    matter how the prefix or suffix are themselves written.

    Args:
        prefix (str): The existing prefix, may be empty.
        suffix (str): The url or next prefix segment to add.

    Returns:
        str: The combined path.
    """
    prefix = prefix.rstrip("/")
    if not suffix or suffix == "/":
        return prefix or "/"
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"{prefix}{suffix}"

def _resolve_handler(
    node: Optional[Node], source: bytes, *, fid: str, fn_by_name: dict
) -> tuple[str, str, Optional[int]]:
    """Work out which function is actually handling a route.

    Only a plain name that matches an already-known top-level function
    gets a real function id; anything else (an inline function, an
    unresolved name) falls back to the file's own id instead of guessing.

    Args:
        node (Optional[Node]): The handler node, or None if there wasn't one.
        source (bytes): Raw file contents.
        fid (str): This file's id, used as a fallback parent id.
        fn_by_name (dict): Known top-level functions, by name.

    Returns:
        tuple[str, str, Optional[int]]: The parent id, display name, and
            line number for this handler.
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
    """Build one finished route or mount record.

    Args:
        node (Node): The call expression this record is based on.
        method (str): The HTTP method, or "MOUNT" for a register() call.
        endpoint (str): The full url, including any prefix.
        parent_id (str): Id of the function or file this belongs to.
        handler (str): Display name of the handler.
        handler_line (Optional[int]): Line number of the handler, if known.
        semantic_type (str): Either "route" or "mount".
        route_kind (str): Either "route" or "mount".
        path (str): File path.
        source (bytes): Raw file contents.
        seen_ids (set[str]): Ids already used; updated in place.

    Returns:
        Statement: The finished record.
    """
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
    """Turn a `{ key: value, ... }` object into a simple lookup table.

    Also understands shorthand properties like `{ prefix }`, which mean
    the same as `{ prefix: prefix }`.

    Args:
        obj_node (Node): The object literal node.
        source (bytes): Raw file contents.

    Returns:
        dict[str, Node]: Each key mapped to its value node.
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
    """Try to turn a piece of code into an actual string value.

    Works for a plain string, a template literal with no `${...}` parts,
    or a variable name that can be traced back to a `const` declaration.

    Args:
        node (Node): The node to resolve.
        source (bytes): Raw file contents.

    Returns:
        Optional[str]: The resolved string, or None if it can't be
            worked out.
    """
    literal = _string_literal(node, source)
    if literal is not None:
        return literal
    if node.type in _IDENTIFIER_LIKE_TYPES:
        return _resolve_in_enclosing_scope(node, node_text(node, source), source)
    return None

def _resolve_in_enclosing_scope(node: Node, name: str, source: bytes) -> Optional[str]:
    """Look up a variable's value by checking each surrounding function.

    Starts at the variable's own position and walks outward one scope
    at a time until a matching `const/let/var` declaration is found.

    Args:
        node (Node): The identifier being looked up.
        name (str): The variable's name.
        source (bytes): Raw file contents.

    Returns:
        Optional[str]: The value found, or None if no declaration matches.
    """
    scope = node.parent
    while scope is not None:
        if scope.type in _SCOPE_NODE_TYPES:
            value = _lookup_const_in_block(scope, name, source)
            if value is not None:
                return value
        scope = scope.parent
    return None

def _lookup_const_in_block(block: Node, name: str, source: bytes) -> Optional[str]:
    """Check one block of code for a `const/let/var NAME = 'value'` line.

    Only looks directly in this block, not inside any blocks nested
    further inside it.

    Args:
        block (Node): The block to search.
        name (str): The variable name to look for.
        source (bytes): Raw file contents.

    Returns:
        Optional[str]: The value found, or None if not declared here.
    """
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
    """Read the plain text out of a string or template literal.

    Args:
        node (Node): The string or template-string node.
        source (bytes): Raw file contents.

    Returns:
        Optional[str]: The literal text, or None if it isn't a plain
            string (e.g. a template with `${...}` inside it).
    """
    if node.type == "string":
        text = node_text(node, source)
        return text[1:-1] if len(text) >= 2 else None
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.children):
            return None
        text = node_text(node, source)
        return text[1:-1] if len(text) >= 2 else None
    return None