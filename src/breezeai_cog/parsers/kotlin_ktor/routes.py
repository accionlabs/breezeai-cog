"""Ktor HTTP route detection from the routing DSL.

Ktor registers routes via a lambda DSL:
    routing {
        get("/path") { ... }
        route("/prefix") {
            post("/sub") { ... }
        }
    }

detect_ktor_routes() walks the AST of an already-parsed FileRecord's tree to find
these call_expression nodes and emit them as Statement records.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text

_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})


def _string_content(node: Node, source: bytes) -> str | None:
    if node.type != "string_literal":
        return None
    parts: list[str] = []
    for child in node.named_children:
        if child.type == "string_content":
            parts.append(node_text(child, source))
        elif child.type in {"string_interpolation", "multiline_string_interpolation"}:
            expr = next(iter(child.named_children), None)
            if expr is not None and expr.type == "simple_identifier":
                parts.append("{" + node_text(expr, source) + "}")
            else:
                parts.append("{param}")
    if parts:
        return "".join(parts)
    # Fallback: strip surrounding quotes from raw text
    raw = node_text(node, source)
    return raw.strip('"') if raw.startswith('"') else None


_SENTINEL_NO_ARGS = object()


def _first_arg(call: Node, source: bytes) -> object:
    """Return the first argument value from a call_expression.

    Returns:
        A string if the arg is a string literal or constant identifier.
        _SENTINEL_NO_ARGS if no value_arguments node exists (trailing-lambda only call).
        None if value_arguments exists but is empty or unresolvable.
    """
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        val_args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
        if val_args is None:
            continue
        first_arg = next((c for c in val_args.named_children if c.type == "value_argument"), None)
        if first_arg is None:
            return None
        # String literal → extract value
        str_lit = next((c for c in first_arg.named_children if c.type == "string_literal"), None)
        if str_lit is not None:
            return _string_content(str_lit, source)
        # Constant reference → wrap as placeholder
        ident = next((c for c in first_arg.named_children if c.type == "simple_identifier"), None)
        if ident is not None:
            return "{" + node_text(ident, source) + "}"
        return None
    return _SENTINEL_NO_ARGS


def _first_string_arg(call: Node, source: bytes) -> str | None:
    """Return the first string-like argument from a call_expression (string or constant)."""
    result = _first_arg(call, source)
    if result is _SENTINEL_NO_ARGS or result is None:
        return None
    return str(result)


def _lambda_body(call: Node) -> Node | None:
    """Return the lambda_literal body of a trailing-lambda call, if present."""
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        annotated = next(
            (c for c in suffix.named_children if c.type == "annotated_lambda"),
            None,
        )
        if annotated is None:
            continue
        return next((c for c in annotated.named_children if c.type == "lambda_literal"), None)
    return None


def _guard_name(call: Node, source: bytes) -> str:
    """``authenticate("jwt")`` → ``authenticate("jwt")``; ``authenticate { }`` → ``authenticate``
    (the default provider). The provider names are kept: they say which check runs."""
    for suffix in call.named_children:
        if suffix.type == "call_suffix":
            args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
            if args is not None and args.named_children:
                return "authenticate" + node_text(args, source)
    return "authenticate"


def _callee_name(call: Node, source: bytes) -> str | None:
    callee = call.named_children[0] if call.named_children else None
    if callee is None or callee.type != "simple_identifier":
        return None
    return node_text(callee, source)


def _collect_routes(
    node: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
    limit: int,
    prefix: str = "",
    existing: dict[str, Statement] | None = None,
    guards: tuple[str, ...] = (),
) -> list[Statement]:
    """Recursively collect Ktor route statements from a subtree. A route whose call the base
    parser already captured (``existing``: id → statement, same start line and column) is
    enriched in place; only routes with no base statement are returned to be appended.
    ``guards`` are the enclosing ``authenticate(...)`` blocks, outermost first."""
    base = existing if existing is not None else {}
    out: list[Statement] = []
    for child in node.named_children:
        if child.type != "call_expression":
            out.extend(_collect_routes(child, source, path, parent_id, seen_ids, limit, prefix, base, guards))
            continue

        verb = _callee_name(child, source)
        if verb is None:
            out.extend(_collect_routes(child, source, path, parent_id, seen_ids, limit, prefix, base, guards))
            continue

        if verb == "authenticate":
            # Guard block: every route inside runs after this authentication check.
            body = _lambda_body(child)
            if body is not None:
                out.extend(_collect_routes(
                    body, source, path, parent_id, seen_ids, limit, prefix, base,
                    (*guards, _guard_name(child, source)),
                ))
            continue

        if verb == "route":
            # Prefix block — push path segment and recurse into lambda body
            sub_path = _first_string_arg(child, source) or ""
            new_prefix = prefix + sub_path
            body = _lambda_body(child)
            if body is not None:
                out.extend(
                    _collect_routes(body, source, path, parent_id, seen_ids, limit, new_prefix, base, guards)
                )
            continue

        if verb not in _HTTP_VERBS:
            out.extend(_collect_routes(child, source, path, parent_id, seen_ids, limit, prefix, base, guards))
            continue

        arg = _first_arg(child, source)
        if arg is _SENTINEL_NO_ARGS:
            # Trailing-lambda only — e.g. `post { }` inside route("/path")
            # Endpoint is just the inherited prefix.
            if not prefix:
                continue
            full_endpoint = prefix
        elif arg is None:
            continue
        else:
            full_endpoint = prefix + str(arg)

        start_line = child.start_point[0] + 1
        col = child.start_point[1]
        end_line = child.end_point[0] + 1
        # The handler is the trailing lambda: anonymous — no name, its line is where it starts.
        body = _lambda_body(child)
        handler_line = body.start_point[0] + 1 if body is not None else None
        captured = base.get(statement_id(path, start_line, col))
        if captured is not None:  # the base parser captured this call: enrich it in place
            captured.semanticType = "route"
            captured.framework = "ktor"
            captured.method = verb.upper()
            captured.endpoint = full_endpoint
            captured.handlerLine = handler_line
            if guards:
                captured.guards = list(guards)
                captured.authRequired = True
            continue
        sid = disambiguate(statement_id(path, start_line, col), seen_ids)
        text = f"{verb}(\"{full_endpoint}\")"
        out.append(Statement(
            id=sid,
            parentId=parent_id,
            nodeType="call_expression",
            semanticType="route",
            framework="ktor",
            text=text[:limit] if limit else text,
            method=verb.upper(),
            endpoint=full_endpoint,
            handlerLine=handler_line,
            guards=list(guards) or None,
            authRequired=True if guards else None,
            startLine=start_line,
            endLine=end_line,
            path=path,
        ))

    return out


def detect_ktor_routes(
    root: Node,
    source: bytes,
    file_path: str,
    file_id: str,
    seen_ids: set[str],
    limit: int = 8000,
    statements: list[Statement] | None = None,
) -> list[Statement]:
    """Walk the full file AST and record every Ktor HTTP route. A route call the base parser
    already captured (in ``statements``) is enriched in place — same span, same id — so a route
    is one statement, not a base statement plus an ``…#2`` copy. Returns the routes that had no
    base statement, to be appended."""
    existing = {s.id: s for s in statements or [] if s.nodeType == "call_expression"}
    return _collect_routes(root, source, file_path, file_id, seen_ids, limit, existing=existing)
