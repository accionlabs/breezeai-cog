"""Flat statement capture for Go (gated by --capture-statements)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint, url_first
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in {"var_declaration", "const_declaration"}:
        # `var x = 1` / `const (A = 1; B = 2)`: the names live on the var_spec/const_spec
        # children (a grouped declaration is named after its first spec).
        specs = [c for c in node.named_children if c.type != "var_spec_list"]
        for group in node.named_children:
            if group.type == "var_spec_list":  # `var ( … )` wraps its specs; `const ( … )` does not
                specs.extend(group.named_children)
        for spec in specs:
            if spec.type in {"var_spec", "const_spec"}:
                name = spec.child_by_field_name("name")
                if name is not None:
                    return node_text(name, source)
    if node.type == "short_var_declaration":
        left = node.child_by_field_name("left")
        if left is not None:
            for child in left.named_children:
                if child.type == "identifier":
                    return node_text(child, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "interpreted_string_literal":
        text = node_text(node, source)
        return text.strip('"')
    if node.type == "raw_string_literal":
        text = node_text(node, source)
        return text.strip('`')
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _is_resty_request(call: Node, source: bytes) -> bool:
    fn = call.child_by_field_name("function")
    operand = fn.child_by_field_name("operand") if fn is not None else None
    while operand is not None and operand.type == _CALL_TYPE:
        inner_fn = operand.child_by_field_name("function")
        if inner_fn is None or inner_fn.type != "selector_expression":
            return False
        field = inner_fn.child_by_field_name("field")
        if field is not None and node_text(field, source) == "R":
            return True
        operand = inner_fn.child_by_field_name("operand")
    return False


def _call_details(
    call: Node, source: bytes, *, resty_imported: bool = False,
) -> tuple[str, str, str | None] | None:
    if call.type != "call_expression":
        return None
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "selector_expression":
        operand = fn.child_by_field_name("operand")
        field = fn.child_by_field_name("field")
        callee = f"{node_text(operand, source)}.{node_text(field, source)}" if operand is not None and field is not None else node_text(fn, source)
        method = node_text(field, source) if field is not None else ""
    else:
        callee = node_text(fn, source)
        method = callee
    args = call.child_by_field_name("arguments")
    arg_nodes: list[Node] = list(args.named_children) if args is not None else []
    resty_request = resty_imported and _is_resty_request(call, source)
    if resty_request:
        callee = f"resty.{callee}"
    endpoint, override = resolve_endpoint(
        arg_nodes, source, _render_url,
        method=method if resty_request else url_first(callee, method),
    )
    if override is not None:
        method = override
    elif method == "Do" and arg_nodes:
        # http.Client.Do(req) / c.Do(req) carries req as its first argument
        req_name = node_text(arg_nodes[0], source)
        req_details = _find_request_definition(call, req_name, source)
        if req_details is not None:
            override_method, resolved_ep = req_details
            if _is_http_client_receiver(call, source):
                callee = "http.Client.Do"
            if override_method:
                method = override_method
            if resolved_ep:
                endpoint = resolved_ep
    return callee, method, endpoint


def _is_http_client_value(node: Node, source: bytes) -> bool:
    while node.type == "unary_expression":
        operand = node.child_by_field_name("operand")
        if operand is None:
            return False
        node = operand
    type_node = node.child_by_field_name("type") if node.type == "composite_literal" else None
    return type_node is not None and node_text(type_node, source) == "http.Client"


def _is_http_client_receiver(call: Node, source: bytes) -> bool:
    fn = call.child_by_field_name("function")
    receiver = fn.child_by_field_name("operand") if fn is not None else None
    if receiver is None:
        return False
    name = node_text(receiver, source)
    if name == "http.DefaultClient":
        return True

    scope: Node | None = call
    while scope is not None and scope.type not in {
        "function_declaration", "method_declaration", "func_literal",
    }:
        scope = scope.parent
    if scope is None:
        return False

    params = scope.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            typ = param.child_by_field_name("type")
            if (
                typ is not None
                and node_text(typ, source).lstrip("*") == "http.Client"
                and any(node_text(item, source) == name for item in param.children_by_field_name("name"))
            ):
                return True

    body = scope.child_by_field_name("body")
    if body is None:
        return False
    stack = [body]
    while stack:
        node = stack.pop()
        if node is not body and node.type == "func_literal":
            continue
        if node.start_byte >= call.start_byte:
            continue
        if node.type in {"short_var_declaration", "var_spec"}:
            for variable, value in _pairs(node) or []:
                if node_text(variable, source) == name and _is_http_client_value(value, source):
                    return True
        stack.extend(node.named_children)
    return False


def _find_request_definition(call: Node, req_name: str, source: bytes) -> tuple[str | None, str | None] | None:
    """Find http.NewRequest("METHOD", url, ...) defining req_name in the enclosing scope."""
    curr: Node | None = call
    while curr is not None and curr.type not in {"function_declaration", "method_declaration", "func_literal"}:
        curr = curr.parent
    if curr is None:
        return None
    body = curr.child_by_field_name("body")
    if body is None:
        return None

    # Go wraps function-body statements in a statement_list node.
    statements = list(body.named_children)
    if len(statements) == 1 and statements[0].type == "statement_list":
        statements = list(statements[0].named_children)

    # Walk statements in body before the call looking for req_name assignment
    target_node = None
    for stmt in statements:
        if stmt.start_byte >= call.start_byte:
            break
        if stmt.type == "short_var_declaration":
            left = stmt.child_by_field_name("left")
            right = stmt.child_by_field_name("right")
            if left is not None and right is not None:
                names = [node_text(c, source) for c in left.named_children if c.type == "identifier"]
                if req_name in names:
                    target_node = right
                    break
        elif stmt.type == "assignment_statement":
            left = stmt.child_by_field_name("left")
            right = stmt.child_by_field_name("right")
            if left is not None and right is not None:
                names = [node_text(c, source) for c in left.named_children if c.type == "identifier"]
                if req_name in names:
                    target_node = right
                    break

    if target_node is None:
        return None

    # Target node could be an expression_list or a call_expression directly
    call_expr = None
    if target_node.type == "call_expression":
        call_expr = target_node
    else:
        for child in target_node.named_children:
            if child.type == "call_expression":
                call_expr = child
                break

    if call_expr is not None:
        fn = call_expr.child_by_field_name("function")
        if fn is not None:
            fn_text = node_text(fn, source)
            if fn_text.endswith("NewRequest") or fn_text.endswith("NewRequestWithContext"):
                args = call_expr.child_by_field_name("arguments")
                if args is not None:
                    arg_list = list(args.named_children)
                    req_method = None
                    req_endpoint = None
                    # http.NewRequest(method, url, body) or http.NewRequestWithContext(ctx, method, url, body)
                    offset = 1 if "WithContext" in fn_text else 0
                    if len(arg_list) > offset:
                        req_method = _render_url(arg_list[offset], source)
                    if len(arg_list) > offset + 1:
                        req_endpoint = _render_url(arg_list[offset + 1], source)
                    return req_method, req_endpoint
    return None


#: Declarations that bind names to values: ``var x = …`` (one ``var_spec`` each) and ``x := …``.
_BINDING_OWNERS = ("var_spec", "short_var_declaration")


def _pairs(owner: Node) -> list[tuple[Node, Node]] | None:
    """(name, value) per binding of a ``var_spec`` / ``short_var_declaration``, or None when
    the names and values do not pair up (``a, b := f()``) or there are no values."""
    if owner.type == "var_spec":
        names = owner.children_by_field_name("name")
        values = owner.child_by_field_name("value")
    else:
        left = owner.child_by_field_name("left")
        names = list(left.named_children) if left is not None else []
        values = owner.child_by_field_name("right")
    if values is None:
        return None
    vals = [v for v in values.named_children if v.type != "comment"]
    if not names or len(names) != len(vals):
        return None
    return list(zip(names, vals))


def bound_name(literal: Node, source: bytes) -> str | None:
    """The variable a ``func_literal`` is stored in (``var notify = func…``, ``cb := func…``),
    else None — an inline literal (an argument, a ``go``/``defer`` call, an assignment)."""
    values = literal.parent
    owner = values.parent if values is not None and values.type == "expression_list" else None
    if owner is None or owner.type not in _BINDING_OWNERS:
        return None
    for name, value in _pairs(owner) or []:
        if (value.start_byte, value.end_byte) == (literal.start_byte, literal.end_byte):
            text = node_text(name, source)
            return text if name.type == "identifier" and text != "_" else None
    return None


def bound_literals(statement: Node) -> list[Node] | None:
    """The function literals a ``var`` / ``:=`` statement stores, when that is all it stores
    (every value is a ``func_literal`` bound to a name), else None."""
    if statement.type == "short_var_declaration":
        owners = [statement]
    elif statement.type == "var_declaration":
        owners = [c for c in statement.named_children if c.type == "var_spec"]
        for lst in (c for c in statement.named_children if c.type == "var_spec_list"):
            owners.extend(c for c in lst.named_children if c.type == "var_spec")
    else:
        return None
    literals: list[Node] = []
    for owner in owners:
        pairs = _pairs(owner)
        if pairs is None:
            return None
        for name, value in pairs:
            if value.type != "func_literal" or name.type != "identifier" or name.text == b"_":
                return None
            literals.append(value)
    return literals or None


def _duplicates_functions(node: Node, barriers: frozenset[tuple[int, int]]) -> bool:
    """True for a ``var`` / ``:=`` statement that only stores function literals extracted as
    Functions (their spans in ``barriers``): the statement would repeat them."""
    literals = bound_literals(node) if barriers else None
    return literals is not None and all((n.start_byte, n.end_byte) in barriers for n in literals)


def _iter_in_scope(
    node: Node, descend_all: bool = False, barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Node]:
    """Statement nodes in ``node``. The walk stops at declared functions and methods (unless
    ``descend_all``) and always at a function literal stored in a variable (``barriers``):
    each is its own Function. Inline literals are walked, so their statements belong to the
    enclosing function, or to the file at package level."""
    out: list[Node] = []
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if (child.start_byte, child.end_byte) in barriers or _duplicates_functions(child, barriers):
            continue
        if child.type in EMIT_TYPES:
            out.append(child)
        out.extend(_iter_in_scope(child, descend_all, barriers))
    return out


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    resty_imported: bool = False,
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node,
                source,
                path,
                parent_id=parent_id,
                limit=limit,
                seen_ids=seen_ids,
                emit_types=EMIT_TYPES,
                control_flow=CONTROL_FLOW,
                call_type=_CALL_TYPE,
                name_of=_name_of,
                call_details=lambda call, src: _call_details(
                    call, src, resty_imported=resty_imported,
                ),
                language="go",
                decorators=[],
            )
        )
    return out
